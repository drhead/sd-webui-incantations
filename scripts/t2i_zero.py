import logging
from os import environ
import modules.scripts as scripts
import gradio as gr
import scipy.stats as stats

from scripts.ui_wrapper import UIWrapper, arg
from modules import script_callbacks
from modules.hypernetworks import hypernetwork
#import modules.sd_hijack_optimizations
from modules.script_callbacks import CFGDenoiserParams
from modules.prompt_parser import reconstruct_multicond_batch
from modules.processing import StableDiffusionProcessing
#from modules.shared import sd_model, opts
from modules.sd_samplers_cfg_denoiser import pad_cond
from modules import shared

import math
import torch
from torch.nn import functional as F
from torchvision.transforms import GaussianBlur

from warnings import warn
from typing import Callable, Dict, Optional
from collections import OrderedDict
import torch

logger = logging.getLogger(__name__)
logger.setLevel(environ.get("SD_WEBUI_LOG_LEVEL", logging.INFO))

"""

Unofficial implementation of algorithms in Multi-Concept T2I-Zero: Tweaking Only The Text Embeddings and Nothing Else

@misc{tunanyan2023multiconcept,
      title={Multi-Concept T2I-Zero: Tweaking Only The Text Embeddings and Nothing Else}, 
      author={Hazarapet Tunanyan and Dejia Xu and Shant Navasardyan and Zhangyang Wang and Humphrey Shi},
      year={2023},
      eprint={2310.07419},
      archivePrefix={arXiv},
      primaryClass={cs.CV}
}

Author: v0xie
GitHub URL: https://github.com/v0xie/sd-webui-semantic-guidance

"""

handles = []

class T2I0StateParams:
        def __init__(self):
                self.window_size_period: int = 10 # [0, 20]
                self.ctnms_alpha: float = 0.05 # [0., 1.] if abs value of difference between uncodition and concept-conditioned is less than this, then zero out the concept-conditioned values less than this
                self.correction_threshold: float = 0.5 # [0., 1.]
                self.correction_strength: float = 0.25 # [0., 1.) # larger bm is less volatile changes in momentum
                self.strength = 1.0
                self.width = None
                self.height = None
                self.dims = []

class T2I0ExtensionScript(UIWrapper):
        def __init__(self):
                self.cached_c = [None, None]
                self.handles = []

        # Extension title in menu UI
        def title(self) -> str:
                return "Multi T2I-Zero"

        # Decide to show menu in txt2img or img2img
        def show(self, is_img2img):
                return scripts.AlwaysVisible

        # Setup menu ui detail
        def setup_ui(self, is_img2img) -> list:
                with gr.Accordion('Multi-Concept T2I-Zero [arXiv:2310.07419v1]', open=True):
                        active = gr.Checkbox(value=False, default=False, label="Active", elem_id='t2i0_active')
                        with gr.Row():
                                window_size = gr.Slider(value = 15, minimum = 0, maximum = 100, step = 1, label="Correction by Similarities Window Size", elem_id = 't2i0_window_size', info="Exclude contribution of tokens further than this from the current token")
                                correction_threshold = gr.Slider(value = 0.5, minimum = 0.0, maximum = 1.0, step = 0.01, label="CbS Score Threshold", elem_id = 't2i0_correction_threshold', info="Filter dimensions with similarity below this threshold")
                                correction_strength = gr.Slider(value = 0.25, minimum = 0.0, maximum = 0.999, step = 0.01, label="CbS Correction Strength", elem_id = 't2i0_correction_strength', info="The strength of the correction, default 0.25")
                        with gr.Row():
                                ctnms_alpha = gr.Slider(value = 0.1, minimum = 0.0, maximum = 1.0, step = 0.01, label="Alpha for Cross-Token Non-Maximum Suppression", elem_id = 't2i0_ctnms_alpha', info="Contribution of the suppressed attention map, default 0.1")
                active.do_not_save_to_config = True
                window_size.do_not_save_to_config = True
                ctnms_alpha.do_not_save_to_config = True
                correction_threshold.do_not_save_to_config = True
                correction_strength.do_not_save_to_config = True
                self.infotext_fields = [
                        (active, lambda d: gr.Checkbox.update(value='T2I-0 Active' in d)),
                        (window_size, 'T2I-0 Window Size'),
                        (ctnms_alpha, 'T2I-0 CTNMS Alpha'),
                        (correction_threshold, 'T2I-0 CbS Score Threshold'),
                        (correction_strength, 'T2I-0 CbS Correction Strength'),
                ]
                self.paste_field_names = [
                        't2i0_active',
                        't2i0_window_size',
                        't2i0_ctnms_alpha',
                        't2i0_correction_threshold',
                        't2i0_correction_strength'
                ]
                return [active, window_size, ctnms_alpha, correction_threshold, correction_strength]

        def process_batch(self, p: StableDiffusionProcessing, *args, **kwargs):
                self.unhook_callbacks()
                self.t2i0_process_batch(p, *args, **kwargs)

        def t2i0_process_batch(self, p: StableDiffusionProcessing, active, window_size, ctnms_alpha, correction_threshold, correction_strength, *args, **kwargs):
                active = getattr(p, "t2i0_active", active)
                if active is False:
                        return
                window_size = getattr(p, "t2i0_window_size", window_size)
                ctnms_alpha = getattr(p, "t2i0_ctnms_alpha", ctnms_alpha)
                correction_threshold = getattr(p, "t2i0_correction_threshold", correction_threshold)
                correction_strength = getattr(p, "t2i0_correction_strength", correction_strength)
                p.extra_generation_params.update({
                        "T2I-0 Active": active,
                        "T2I-0 window_size Period": window_size,
                        "T2I-0 CTNMS Alpha": ctnms_alpha,
                        "T2I-0 CbS Score Threshold": correction_threshold,
                        "T2I-0 CbS Correction Strength": correction_strength,
                })

                self.create_hook(p, active, window_size, ctnms_alpha, correction_threshold, correction_strength, p.width, p.height)

        def parse_concept_prompt(self, prompt:str) -> list[str]:
                """
                Separate prompt by comma into a list of concepts
                TODO: parse prompt into a list of concepts using A1111 functions
                >>> g = lambda prompt: self.parse_concept_prompt(prompt)
                >>> g("")
                []
                >>> g("apples")
                ['apples']
                >>> g("apple, banana, carrot")
                ['apple', 'banana', 'carrot']
                """
                if len(prompt) == 0:
                        return []
                return [x.strip() for x in prompt.split(",")]

        def create_hook(self, p, active, window_size, ctnms_alpha, correction_threshold, correction_strength, width, height, *args, **kwargs):
                # Create a list of parameters for each concept
                t2i0_params = []

                #for _, strength in concept_conds:
                params = T2I0StateParams()
                params.window_size_period = window_size
                params.ctnms_alpha = ctnms_alpha
                params.correction_threshold = correction_threshold
                params.correction_strength = correction_strength
                params.strength = 1.0
                params.width = width
                params.height = height 
                params.dims = [width, height]
                t2i0_params.append(params)

                # Use lambda to call the callback function with the parameters to avoid global variables
                y = lambda params: self.on_cfg_denoiser_callback(params, t2i0_params)
                un = lambda params: self.unhook_callbacks()

                # Hook callbacks
                if ctnms_alpha > 0:
                        self.ready_hijack_forward(ctnms_alpha, width, height)

                logger.debug('Hooked callbacks')
                script_callbacks.on_cfg_denoiser(y)
                script_callbacks.on_script_unloaded(self.unhook_callbacks)

        def postprocess_batch(self, p, *args, **kwargs):
                self.t2i0_postprocess_batch(p, *args, **kwargs)

        def t2i0_postprocess_batch(self, p, active, *args, **kwargs):
                self.unhook_callbacks()
                active = getattr(p, "t2i0_active", active)
                if active is False:
                        return

        def unhook_callbacks(self):
                global handles
                logger.debug('Unhooked callbacks')
                cross_attn_modules = self.get_cross_attn_modules()
                for module in cross_attn_modules:
                        _remove_all_forward_hooks(module, 'cross_token_non_maximum_suppression')
                script_callbacks.remove_current_script_callbacks()

        def correction_by_similarities(self, f, C, percentile, gamma, alpha):
                """
                Apply the Correction by Similarities algorithm on embeddings.

                Args:
                f (Tensor): The embedding tensor of shape (n, d).
                C (list): Indices of selected tokens.
                percentile (float): Percentile to use for score threshold.
                gamma (int): Window size for the windowing function.
                alpha (float): Correction strength.

                Returns:
                Tensor: The corrected embedding tensor.
                """
                if alpha == 0:
                        return f

                n, d = f.shape
                f_tilde = f.detach().clone()  # Copy the embedding tensor

                # Define a windowing function
                def psi(c, gamma, n, dtype, device):
                        window = torch.zeros(n, dtype=dtype, device=device)
                        start = max(0, c - gamma)
                        end = min(n, c + gamma + 1)
                        window[start:end] = 1
                        return window

                for token_idx, c in enumerate(C):
                        Sc = f[c] * f  # Element-wise multiplication
                        Sc_flat_positive = Sc[Sc > 0] # product = greater positive value indicates more similarity, filter out values under score threshold from 0 to max

                        # calculate score threshold to filter out values under score threshold
                        # often there is a huge difference between the max and min values, so we use a log-like function instead
                        k = 10
                        e= 2.718281
                        pct = min(0.999999999, max(0.000001, 1 - e**(-k * percentile)))
                        tau = torch.quantile(Sc_flat_positive, pct)

                        Sc_tilde = Sc * (Sc > tau)  # Apply threshold and filter
                        Sc_tilde /= Sc_tilde.max()  # Normalize

                        window = psi(c, gamma, n, Sc_tilde.dtype, Sc_tilde.device).unsqueeze(1)  # Apply windowing function
                        Sc_tilde *= window
                        f_c_tilde = torch.sum(Sc_tilde * f, dim=0)  # Combine embeddings
                        f_tilde[c] = (1 - alpha) * f[c] + alpha * f_c_tilde  # Blend embeddings
                return f_tilde

        def ready_hijack_forward(self, alpha, width, height):
                """ Create a hook to modify the output of the forward pass of the cross attention module 
                Only modifies the output of the cross attention modules that get context (i.e. text embedding)
                """
                cross_attn_modules = self.get_cross_attn_modules()

                def cross_token_non_maximum_suppression(module, input, kwargs, output):
                        context = kwargs.get('context', None)
                        if context is None:
                                return
                        batch_size, sequence_length, inner_dim = output.shape

                        max_dims = width*height
                        factor = math.isqrt(max_dims // sequence_length) # should be a square of 2
                        downscale_width = width // factor
                        downscale_height = height // factor
                        if downscale_width * downscale_height != sequence_length:
                                print(f"Error: Width: {width}, height: {height}, Downscale width: {downscale_width}, height: {downscale_height}, Factor: {factor}, Max dims: {max_dims}\n")
                                return

                        h = module.heads
                        head_dim = inner_dim // h
                        dtype = output.dtype
                        device = output.device

                        # Reshape the attention map to batch_size, height, width
                        # FIXME: need to assert the height/width divides into the sequence length
                        attention_map = output.view(batch_size, downscale_height, downscale_width, inner_dim)

                        # Select token indices (Assuming this is provided as t2i0_params or similar)
                        selected_tokens = torch.tensor(list(range(inner_dim)))  # Example: Replace with actual indices

                        # Extract and process the selected attention maps
                        # GaussianBlur expects the input [..., C, H, W]
                        gaussian_blur = GaussianBlur(kernel_size=3, sigma=1)
                        AC = attention_map[:, :, :, selected_tokens]  # Extracting relevant attention maps
                        AC = AC.permute(0, 3, 1, 2)
                        AC = gaussian_blur(AC)  # Applying Gaussian smoothing
                        AC = AC.permute(0, 2, 3, 1)

                        # Find the maximum contributing token for each pixel
                        M = torch.argmax(AC, dim=-1)

                        # Create one-hot vectors for suppression
                        t = attention_map.size(-1)
                        one_hot_M = F.one_hot(M, num_classes=t).to(dtype=dtype, device=device)

                        # Apply the suppression mask
                        #suppressed_attention_map = one_hot_M.unsqueeze(2) * attention_map
                        suppressed_attention_map = one_hot_M * attention_map

                        # Reshape back to original dimensions
                        suppressed_attention_map = suppressed_attention_map.view(batch_size, sequence_length, inner_dim)

                        out_tensor = (1-alpha) * output + alpha * suppressed_attention_map

                        return out_tensor
                # Hook
                for module in cross_attn_modules:
                        handle = module.register_forward_hook(cross_token_non_maximum_suppression, with_kwargs=True)

        def get_cross_attn_modules(self):
                """ Get all cross attention modules """
                m = shared.sd_model
                nlm = m.network_layer_mapping
                cross_attn_modules = [m for m in nlm.values() if 'CrossAttention' in m.__class__.__name__]
                return cross_attn_modules

        def on_cfg_denoiser_callback(self, params: CFGDenoiserParams, t2i0_params: list[T2I0StateParams]):
                if isinstance(params.text_cond, dict):
                        text_cond = params.text_cond['crossattn'] # SD XL
                else:
                        text_cond = params.text_cond # SD 1.5

                sp = t2i0_params[0]
                window_size = sp.window_size_period
                correction_strength = sp.correction_strength
                score_threshold = sp.correction_threshold
                width = sp.width
                height = sp.height
                ctnms_alpha = sp.ctnms_alpha

                for batch_idx, batch in enumerate(text_cond):
                        window = list(range(0, len(batch)))
                        f_bar = self.correction_by_similarities(batch, window, score_threshold, window_size, correction_strength)
                        if isinstance(params.text_cond, dict):
                                params.text_cond['crossattn'][batch_idx] = f_bar
                        else:
                                params.text_cond[batch_idx] = f_bar
                return

        def get_xyz_axis_options(self) -> dict:
                xyz_grid = [x for x in scripts.scripts_data if x.script_class.__module__ == "xyz_grid.py"][0].module
                extra_axis_options = {
                        xyz_grid.AxisOption("[T2I-0] Active", str, t2i0_apply_override('t2i0_active', boolean=True), choices=xyz_grid.boolean_choice(reverse=True)),
                        xyz_grid.AxisOption("[T2I-0] ctnms_alpha", float, t2i0_apply_field("t2i0_ctnms_alpha")),
                        xyz_grid.AxisOption("[T2I-0] Window Size", int, t2i0_apply_field("t2i0_window_size")),
                        xyz_grid.AxisOption("[T2I-0] Correction Threshold", float, t2i0_apply_field("t2i0_correction_threshold")),
                        xyz_grid.AxisOption("[T2I-0] Correction Strength", float, t2i0_apply_field("t2i0_correction_strength")),
                }
                return extra_axis_options

# XYZ Plot
# Based on @mcmonkey4eva's XYZ Plot implementation here: https://github.com/mcmonkeyprojects/sd-dynamic-thresholding/blob/master/scripts/dynamic_thresholding.py
def t2i0_apply_override(field, boolean: bool = False):
    def fun(p, x, xs):
        if boolean:
            x = True if x.lower() == "true" else False
        setattr(p, field, x)
    return fun

def t2i0_apply_field(field):
    def fun(p, x, xs):
        if not hasattr(p, "t2i0_active"):
                setattr(p, "t2i0_active", True)
        setattr(p, field, x)
    return fun


# thanks torch; removing hooks DOESN'T WORK
# thank you to @ProGamerGov for this https://github.com/pytorch/pytorch/issues/70455
def _remove_all_forward_hooks(
    module: torch.nn.Module, hook_fn_name: Optional[str] = None
) -> None:
    """
    This function removes all forward hooks in the specified module, without requiring
    any hook handles. This lets us clean up & remove any hooks that weren't property
    deleted.

    Warning: Various PyTorch modules and systems make use of hooks, and thus extreme
    caution should be exercised when removing all hooks. Users are recommended to give
    their hook function a unique name that can be used to safely identify and remove
    the target forward hooks.

    Args:

        module (nn.Module): The module instance to remove forward hooks from.
        hook_fn_name (str, optional): Optionally only remove specific forward hooks
            based on their function's __name__ attribute.
            Default: None
    """

    if hook_fn_name is None:
        warn("Removing all active hooks can break some PyTorch modules & systems.")


    def _remove_hooks(m: torch.nn.Module, name: Optional[str] = None) -> None:
        if hasattr(module, "_forward_hooks"):
            if m._forward_hooks != OrderedDict():
                if name is not None:
                    dict_items = list(m._forward_hooks.items())
                    m._forward_hooks = OrderedDict(
                        [(i, fn) for i, fn in dict_items if fn.__name__ != name]
                    )
                else:
                    m._forward_hooks: Dict[int, Callable] = OrderedDict()

    def _remove_child_hooks(
        target_module: torch.nn.Module, hook_name: Optional[str] = None
    ) -> None:
        for name, child in target_module._modules.items():
            if child is not None:
                _remove_hooks(child, hook_name)
                _remove_child_hooks(child, hook_name)

    # Remove hooks from target submodules
    _remove_child_hooks(module, hook_fn_name)

    # Remove hooks from the target module
    _remove_hooks(module, hook_fn_name)