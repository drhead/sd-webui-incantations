"""
MIT License

Copyright (c) 2022 Yuxin Wen and Neel Jain

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# copied and modified from https://github.com/YuxinWenRick/hard-prompts-made-easy/blob/main/optim_utils.py and https://github.com/YuxinWenRick/hard-prompts-made-easy/blob/f22a1bec01991d94697304443cacbd66e0167e6b/open_clip/model.py 
# commit f22a1be

import random
import numpy as np
import requests
import re
from io import BytesIO
from os import environ
from PIL import Image
from statistics import mean
import logging
import copy
import json
from typing import Any, Mapping
from modules import shared

import open_clip
import clip

import torch
from scipy.optimize import fmin_l_bfgs_b

from sentence_transformers.util import (semantic_search, 
                                        dot_score, 
                                        normalize_embeddings)

default_args = {
    "prompt_len": 16,
    "iter": 1000,
    "lr": 0.1,
    "weight_decay": 0.1,
    "prompt_bs": 1,
    "print_step": 100,
    "batch_size": 1,
    "print_new_best": False,
    "print_new_best_sum": True,
    "loss_weight": 1.0,
    "clip_model": "ViT-L/14",
    "pretrained": "openai",
}


def read_json(filename: str) -> Mapping[str, Any]:
    """Returns a Python dict representation of JSON object at input file."""
    with open(filename) as fp:
        return json.load(fp)


def nn_project(curr_embeds, embedding_layer, embedding_matrix, print_hits=False):
    with torch.no_grad():
        bsz,seq_len,emb_dim = curr_embeds.shape
        
        # Using the sentence transformers semantic search which is 
        # a dot product exact kNN search between a set of 
        # query vectors and a corpus of vectors
        curr_embeds = curr_embeds.reshape((-1,emb_dim))
        curr_embeds = normalize_embeddings(curr_embeds) # queries

        # embedding_matrix = embedding_layer.weight
        # embedding_matrix = normalize_embeddings(embedding_matrix)
        
        hits = semantic_search(curr_embeds, embedding_matrix, 
                                query_chunk_size=curr_embeds.shape[0], 
                                top_k=1,
                                score_function=dot_score)

        # if print_hits:
        #     all_hits = []
        #     for hit in hits:
        #         all_hits.append(hit[0]["score"])
        #     print(f"mean hits:{mean(all_hits)}")
        
        nn_indices = torch.tensor([hit[0]["corpus_id"] for hit in hits], device=curr_embeds.device)
        nn_indices = nn_indices.reshape((bsz,seq_len))

        projected_embeds = embedding_layer(nn_indices)

    return projected_embeds, nn_indices


def set_random_seed(seed=0):
    torch.manual_seed(seed + 0)
    torch.cuda.manual_seed(seed + 1)
    torch.cuda.manual_seed_all(seed + 2)
    np.random.seed(seed + 3)
    torch.cuda.manual_seed_all(seed + 4)
    random.seed(seed + 5)


def decode_ids(input_ids, tokenizer, by_token=False):
    input_ids = input_ids.detach().cpu().numpy()

    texts = []

    if by_token:
        for input_ids_i in input_ids:
            curr_text = []
            for tmp in input_ids_i:
                curr_text.append(tokenizer.decode([tmp]))

            texts.append('|'.join(curr_text))
    else:
        for input_ids_i in input_ids:
            texts.append(tokenizer.decode(input_ids_i))

    return texts


def dowload_image(url):
    try:
        response = requests.get(url)
    except:
        return None
    return Image.open(BytesIO(response.content)).convert("RGB")


def get_text_feature(model, tokenizer_funct, device, target_prompts=None):
    if isinstance(target_prompts, list):
        target_prompts = " ".join(target_prompts)
    texts = tokenizer_funct(target_prompts).to(device)
    text_target_features = model.encode_text(texts)
    return text_target_features


def get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, args, device, target_prompts=None):
    """ Returns padded text embedding and unpadded text embedding """
    # dummy_ids -> padded ids
    # prompt_embeds = padded prompt
    if target_prompts is None:
        return None, None, None
    if isinstance(target_prompts, list):
        if len(target_prompts) == 0:
            return None, None, None

    bos_token = getattr(tokenizer, "bos_token", "<start_of_text>")
    bos_token_id = getattr(tokenizer, "bos_token_id", 49406)
    eos_token = getattr(tokenizer, "eos_token", "<end_of_text>")
    eos_token_id = getattr(tokenizer, "eos_token_id", 49407)
    pad_token = getattr(tokenizer, "pad_token", "<end_of_text>")
    pad_token_id = getattr(tokenizer, "pad_token_id", 49407)
    model_max_len = getattr(tokenizer, "model_max_length", 77)

    if isinstance(target_prompts, list):
        target_prompts = " ".join(target_prompts)
    # randomly optimize prompt embeddings
    template_text = f"{target_prompts}"
    dummy_ids = tokenizer.encode(template_text)

    unpadded_prompt_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)
    unpadded_prompt_embeds = unpadded_prompt_ids.to(device)
    unpadded_prompt_embeds = token_embedding(unpadded_prompt_ids).detach()
    unpadded_prompt_embeds.requires_grad = False

    #dummy_ids = [bos_token_id] + dummy_ids + [eos_token_id]
    dummy_ids += [0] * (model_max_len - len(dummy_ids))
    #dummy_ids += [pad_token_id] * (model_max_len - len(dummy_ids))
    dummy_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)

    prompt_ids = dummy_ids.to(device)
    prompt_embeds = token_embedding(prompt_ids).detach()
    prompt_embeds.requires_grad = False

    return dummy_ids, prompt_embeds, unpadded_prompt_embeds


def get_target_feature(model, preprocess, tokenizer_funct, device, target_images=None, target_prompts=None):
    """ Encode target images into features
    Args:
        model: CLIP model
        preprocess: image preprocessor
        tokenizer_funct (callable): tokenizer function
        device (str): torch device
        target_images (Image): PIL Image
    
    Returns:
    tensor: encoded image embedding
    """
    image_target_features = None
    text_target_features = None
    if target_images is not None:
        with torch.no_grad():
            curr_images = [preprocess(i).unsqueeze(0) for i in target_images]
            curr_images = torch.concatenate(curr_images).to(device)
            image_target_features = model.encode_image(curr_images)
    return image_target_features


def initialize_prompt_init(tokenizer, token_embedding, prompt, args, device, random_prompt=False):
    bos_token = getattr(tokenizer, "bos_token", "<start_of_text>")
    bos_token_id = getattr(tokenizer, "bos_token_id", 49406)
    eos_token = getattr(tokenizer, "eos_token", "<end_of_text>")
    eos_token_id = getattr(tokenizer, "eos_token_id", 49407)
    pad_token = getattr(tokenizer, "pad_token", "<end_of_text>")
    pad_token_id = getattr(tokenizer, "pad_token_id", 49407)
    model_max_len = getattr(tokenizer, "model_max_length", 77)
    vocab_size = getattr(tokenizer, "vocab_size", 49408)
    prompt_len = args["prompt_len"]

    # randomly optimize prompt embeddings
    template_text = f"{prompt}"
    encoded_prompt = tokenizer.encode(template_text)
    prompt_len = len(encoded_prompt)
    # encoded_prompt = [bos_token_id] + encoded_prompt + [eos_token_id]

    if random_prompt:
        prompt_len = args["prompt_len"]
        prompt_ids = torch.randint(vocab_size, (args["prompt_bs"], prompt_len)).to(device)
    else:
        prompt_ids = torch.tensor([encoded_prompt] * args["prompt_bs"]).to(device)

    prompt_embeds = token_embedding(prompt_ids).detach()
    prompt_embeds.requires_grad = True

    # initialize the template
    template_text = "{}"
    padded_template_text = template_text.format(" ".join([bos_token] * prompt_len))
    dummy_ids = tokenizer.encode(padded_template_text)
    # if using the model's tokenizer, check to see if bos_token and eos_token are in the dummy_ids
    # dummy_ids = dummy_ids[1:-1]

    # -1 for optimized tokens
    dummy_ids = [i if i != bos_token_id else -1 for i in dummy_ids]
    dummy_ids = [bos_token_id] + dummy_ids + [eos_token_id]
    dummy_ids += [0] * (model_max_len - len(dummy_ids))
    dummy_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)
    #dummy_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)

    # for getting dummy embeds; -1 won't work for token_embedding
    tmp_dummy_ids = copy.deepcopy(dummy_ids)
    tmp_dummy_ids[tmp_dummy_ids == -1] = 0
    dummy_embeds = token_embedding(tmp_dummy_ids).detach()
    dummy_embeds.requires_grad = False
    
    return prompt_embeds, dummy_embeds, dummy_ids

def initialize_prompt(tokenizer, token_embedding, args, device):
    bos_token = getattr(tokenizer, "bos_token", "<start_of_text>")
    bos_token_id = getattr(tokenizer, "bos_token_id", 49406)
    eos_token = getattr(tokenizer, "eos_token", "<end_of_text>")
    eos_token_id = getattr(tokenizer, "eos_token_id", 49407)
    pad_token = getattr(tokenizer, "pad_token", "<end_of_text>")
    pad_token_id = getattr(tokenizer, "pad_token_id", 49407)
    model_max_len = getattr(tokenizer, "model_max_length", 77)
    vocab_size = getattr(tokenizer, "vocab_size", 49408)
    prompt_len = args["prompt_len"]

    # randomly optimize prompt embeddings
    prompt_ids = torch.randint(vocab_size, (args["prompt_bs"], prompt_len)).to(device)
    prompt_embeds = token_embedding(prompt_ids).detach()
    prompt_embeds.requires_grad = True

    # initialize the template
    template_text = "{}"
    padded_template_text = template_text.format(" ".join([bos_token] * prompt_len))
    dummy_ids = tokenizer.encode(padded_template_text)
    # if using the model's tokenizer, check to see if bos_token and eos_token are in the dummy_ids
    # dummy_ids = dummy_ids[1:-1]

    # -1 for optimized tokens
    dummy_ids = [i if i != bos_token_id else -1 for i in dummy_ids]
    dummy_ids = [bos_token_id] + dummy_ids + [eos_token_id]
    dummy_ids += [pad_token_id] * (model_max_len - len(dummy_ids))
    dummy_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)
    #dummy_ids = torch.tensor([dummy_ids] * args["prompt_bs"]).to(device)

    # for getting dummy embeds; -1 won't work for token_embedding
    tmp_dummy_ids = copy.deepcopy(dummy_ids)
    tmp_dummy_ids[tmp_dummy_ids == -1] = 0
    dummy_embeds = token_embedding(tmp_dummy_ids).detach()
    dummy_embeds.requires_grad = False
    
    return prompt_embeds, dummy_embeds, dummy_ids


def optimize_prompt_loop_s4a(model, tokenizer, token_embedding, img_coarse_features, img_fine_features, original_prompt, args, device, tokenizer_funct):

    with torch.no_grad():
        embedding_matrix = token_embedding.weight
        embedding_matrix = normalize_embeddings(embedding_matrix)

        dash_token = "-"
        dash_token_emb = get_text_feature(model, tokenizer_funct, device, target_prompts='-')

        # constant gradient from coarse to fine
        _img_fine_features = img_fine_features / img_fine_features.norm(dim=1, keepdim=True)
        _img_coarse_features = img_coarse_features / img_coarse_features.norm(dim=1, keepdim=True)
        grad_img_features = _img_fine_features - _img_coarse_features

        # original prompt
        og_prompt_embeds, og_dummy_embeds, og_dummy_ids = initialize_prompt_init(tokenizer, token_embedding, original_prompt, args, device)
        p_bs, p_len, p_dim = og_prompt_embeds.shape
        #og_text_features = model.forward_text_embedding(model, og_prompt_embeds, og_dummy_ids, img_fine_features, return_feature=True)

        og_prompt_embeds.requires_grad = False
        og_dummy_embeds.requires_grad = False
        og_dummy_ids.requires_grad = False

        og_prompt_embeds_padded = og_dummy_embeds.detach().clone()
        og_prompt_embeds_padded[og_dummy_ids == -1] = og_prompt_embeds.reshape(-1, p_dim)
        # og_prompt_embeds_padded.requires_grad = False

        og_text_features = encode_text_embedding(model, og_prompt_embeds_padded, og_dummy_ids, avg_text=False)

        # [batch size, prompt len, prompt dim]
        masked_p_len = min(p_len * 2, 77-2)
        masked_prompt_ids = [49406] + [-2] * (p_len) + [-1] * (p_len) + [49407] + [0] * (77 - masked_p_len - 2)
        if len(masked_prompt_ids) > 77:
            masked_prompt_ids = masked_prompt_ids[0:77]
            masked_prompt_ids[-1] = 49407
        masked_prompt_ids = torch.tensor([masked_prompt_ids] * args["prompt_bs"]).to(device)
        #masked_prompt_tokens = [49406] + [-1] * (masked_p_len) + [49407] + [0] * (77 - masked_p_len - 2)

        masked_prompt_embeds = semantic_guidance(og_prompt_embeds, img_fine_features, 0.5, dash_token_emb)
        masked_prompt_embeds_padded = og_dummy_embeds.detach().clone()
        masked_prompt_embeds_padded[og_dummy_ids == -1] = og_prompt_embeds.reshape(-1, p_dim)
        masked_prompt_embeds_padded[masked_prompt_ids == -1] = masked_prompt_embeds.reshape(-1, p_dim)

        masked_prompt_text_features = encode_text_embedding(model, masked_prompt_embeds_padded, masked_prompt_ids, avg_text=False)


    # trainable prompt
    # TODO : proper concat of prompt
    prompt_embeds, dummy_embeds, dummy_ids = initialize_prompt_init(tokenizer, token_embedding, original_prompt, args, device, random_prompt=False)

    opt_iters = args["iter"]
    lr = args["lr"]
    weight_decay = args["weight_decay"]
    print_step = args["print_step"]
    batch_size = args["batch_size"]
    print_new_best = args["print_new_best"]
    print_new_best_sum = args["print_new_best_sum"]

    # initialize prompt
    #prompt_embeds, dummy_embeds, dummy_ids = initialize_prompt(tokenizer, token_embedding, args, device)
    p_bs, p_len, p_dim = prompt_embeds.shape

    # get optimizer
    input_optimizer = torch.optim.AdamW([prompt_embeds], lr=lr, maximize=False)
    #kinput_optimizer = torch.optim.AdamW([prompt_embeds], lr=lr, weight_decay=weight_decay)

    best_sum =  -1000 * args["loss_weight"]
    best_qual = -1000 * args["loss_qual"] # sparsity loss 
    best_sem =  -1000 * args["loss_sem"] # sparsity loss 
    best_tt =   -1000 * args["loss_tt"]
    best_ti =   -1000 * args["loss_ti"]
    best_spar = 1000 * args["loss_spar"] # sparsity loss 

    best_text = ""
    best_text_qual = ""
    best_text_sem = ""
    best_text_tt = ""
    best_text_ti = ""
    best_text_spar = ""

    regex = re.compile(r"<[|]?end[_]?of[_]?text[|]?>")

    for step in range(opt_iters):
        
        # forward projection to find matching tokens
        projected_embeds, nn_indices = nn_project(prompt_embeds, token_embedding, embedding_matrix, print_hits=False)

        tmp_embeds = prompt_embeds.detach().clone()
        tmp_embeds.data = projected_embeds.data
        tmp_embeds.requires_grad = True
        
        # padding
        # padded_embeds = copy.deepcopy(dummy_embeds)
        padded_embeds = dummy_embeds.detach().clone()
        padded_embeds[dummy_ids == -1] = tmp_embeds.reshape(-1, p_dim)

        # tt_loss = 0
        # spar_loss = 0

        #if text_target_features is not None:
        # with torch.no_grad():
        #     combined_embeddings = combine_embeddings(tmp_embeds, og_prompt_embeds)

        # forward
        #text_features = encode_text_embedding(model, padded_embeds, dummy_ids, avg_text=False)
        text_features = model.forward_text_embedding(model, padded_embeds, dummy_ids, img_fine_features, return_feature=True)

        # quality loss
        #qual_loss = quality_loss(grad_img_features, og_text_features, text_features)
        qual_loss = quality_loss(grad_img_features, og_text_features, text_features)

        # semantic guidance loss
        sem_loss = semantic_loss(text_features, masked_prompt_text_features)

        # text-img loss between orig+trained prompt and image
        #ti_loss = text_image_loss(img_fine_features, padded_embeds)
        ti_loss = text_image_loss(img_fine_features, text_features)

        # text-text loss - this works for sure
        #tt_loss = text_text_loss(og_prompt_embeds, tmp_embeds)
        tt_loss = text_text_loss(og_text_features, text_features)

        # sparsity loss - unsure if this is correct
        spar_loss = sparsity_loss(og_prompt_embeds)
        #spar_loss = sparsity_loss(padded_embeds)

        # loss
        #loss =  1 - (qual_loss * args["loss_qual"])
        #loss += 1 - (sem_loss  * args["loss_sem"])
        #loss += 1 - (ti_loss   * args["loss_ti"])
        #loss += 1 - (tt_loss   * args["loss_tt"])
        #loss += (spar_loss * args["loss_spar"])
        loss =  (qual_loss * args["loss_qual"])
        loss += (sem_loss  * args["loss_sem"])
        loss += (ti_loss   * args["loss_ti"])
        loss += (tt_loss   * args["loss_tt"])
        loss += (spar_loss * args["loss_spar"])
        loss = loss * args["loss_weight"]

        #loss.backward()

        prompt_embeds.grad, = torch.autograd.grad(loss, [tmp_embeds])
        
        input_optimizer.step()
        input_optimizer.zero_grad()

        curr_lr = input_optimizer.param_groups[0]["lr"]

        best_indx = 0
        decoded_text = decode_ids(nn_indices, tokenizer)[best_indx]
        decoded_text = regex.sub("", decoded_text)

        total_loss = \
            + (qual_loss * args["loss_qual"]) \
            + (sem_loss * args["loss_sem"]) \
            + (tt_loss * args["loss_tt"]) \
            + (ti_loss * args["loss_ti"]) \
            - (spar_loss * args["loss_spar"])

        if print_step is not None and (step % print_step == 0 or step == opt_iters-1):
            per_step_message = f"step: {step}, lr: {curr_lr}"
            # if not print_new_best:
            per_step_message = f"\n{per_step_message}, total: {total_loss:.3f}, qual: {qual_loss:.3f}, sem: {sem_loss:.3f}, tt: {tt_loss:.3f}, ti: {ti_loss:.3f}, spar: {spar_loss:.3f}\n text: {decoded_text}"
            print(per_step_message)

        if best_qual * args["loss_qual"] < qual_loss * args["loss_qual"]:
            best_qual = qual_loss * args["loss_qual"]
            best_text_qual = decoded_text
            if print_new_best:
                print(f"new best qual loss: {best_qual}")
                print(f"new best qual: {best_text_qual}")

        if best_sem * args["loss_sem"] < sem_loss * args["loss_sem"]:
            best_sem = sem_loss * args["loss_sem"]
            best_text_sem = decoded_text
            if print_new_best:
                print(f"new best sem loss: {best_sem}")
                print(f"new best sem: {best_text_sem}")

        if best_tt * args["loss_tt"] < tt_loss * args["loss_tt"]:
            best_tt = tt_loss * args["loss_tt"]
            best_text_tt = decoded_text
            if print_new_best:
                print(f"new best tt loss: {best_tt}")
                print(f"new best tt: {best_text_tt}")

        if best_ti * args["loss_ti"] < ti_loss * args["loss_ti"]:
            best_ti = ti_loss * args["loss_ti"]
            best_text_ti = decoded_text
            if print_new_best:
                print(f"new best ti loss: {best_ti}")
                print(f"new best ti: {best_text_ti}")

        if best_spar * args["loss_spar"] > spar_loss * args["loss_spar"]:
            best_spar = spar_loss * args["loss_spar"]
            best_text_spar = decoded_text
            if print_new_best:
                print(f"new best spar loss: {best_spar}")
                print(f"new best spar: {best_text_spar}")

        if best_sum < total_loss:
            best_sum = total_loss
            best_text = decoded_text
            if print_new_best or print_new_best_sum:
                print(f"new best sum: {best_sum}")
                print(f"new best prompt: {best_text}")


    if print_step is not None:
        print(f"sum: {best_sum}, qual: {best_qual}, sem: {best_sem}, tt: {best_tt}, ti: {best_ti}, spar: {best_spar}")
        print(f"best prompt: {best_text}")
        print(f"best prompt qual: {best_text_qual}")
        print(f"best prompt sem: {best_text_sem}")
        print(f"best prompt tt: {best_text_tt}")
        print(f"best prompt ti: {best_text_ti}")
        print(f"best prompt spar: {best_text_spar}")

    return best_text


def quality_loss(grad_img_features, original_text_emb, modified_text_emb) -> float:
    """ grad_img_features is the unchanging gradient from coarse to fine """
    # larger number means more similarity
    original = original_text_emb / original_text_emb.norm(dim=-1, keepdim=True)
    modified = modified_text_emb / modified_text_emb.norm(dim=-1, keepdim=True)
    grad_text_text = modified - original 
    loss = (grad_img_features * grad_text_text.t()).sum(dim=-1).mean()
    #loss = (grad_img_features * grad_text_text).sum(dim=-1).mean()
    return loss


def text_text_loss(original_text_emb, modified_text_emb) -> float:
    # larger number means more similarity
    original = original_text_emb / original_text_emb.norm(dim=-1, keepdim=True)
    modified = modified_text_emb / modified_text_emb.norm(dim=-1, keepdim=True)
    loss = (modified * original.t()).sum(dim=-1).mean() # actually cosine similarity
    #loss = (modified * original).sum(dim=-1).mean() # actually cosine similarity
    return loss


def text_image_loss(image_emb, modified_text_emb) -> float:
    # larger number means more similarity
    image_norm = image_emb / image_emb.norm(dim=-1, keepdim=True)
    text_norm = modified_text_emb / modified_text_emb.norm(dim=-1, keepdim=True)
    loss = (text_norm * image_norm.t()).sum(dim=-1).mean()
    #loss = (text_norm * image_norm).sum(dim=-1).mean()
    return loss


def combine_embeddings(tensor, optional_tensor=None) -> float:
    if optional_tensor is not None:
        orig_emb = optional_tensor
        batch_size = tensor.shape[0]

        if batch_size > optional_tensor.shape[0]:
            orig_emb = torch.stack([optional_tensor] * batch_size)

        text_embeddings = torch.cat((orig_emb, tensor), dim=1)
    else:
        text_embeddings = tensor
    return text_embeddings


def optimize_prompt_loop_builtin(model, tokenizer, token_embedding, all_target_features, prompt_ids, text_target_features, unpadded_prompt_embeds, args, device, tokenizer_funct):
    #
    # DON'T EDIT ME
    #
    #m = shared.sd_model
    # tokenizer = m.cond_stage_model.tokenizer
    # tokenizer_funct = open_clip.tokenizer.tokenize

    opt_iters = args["iter"]
    lr = args["lr"]
    weight_decay = args["weight_decay"]
    print_step = args["print_step"]
    batch_size = args["batch_size"]
    print_new_best = args["print_new_best"]

    # initialize prompt
    prompt_embeds, dummy_embeds, dummy_ids = initialize_prompt(tokenizer, token_embedding, args, device)
    p_bs, p_len, p_dim = prompt_embeds.shape

    # get optimizer
    input_optimizer = torch.optim.AdamW([prompt_embeds], lr=lr, weight_decay=weight_decay)

    best_sum =  -1000 * args["loss_weight"]
    best_sim =  -1000 * args["loss_weight"]
    best_tt =   -1000 * args["loss_tt"]
    best_ti =   -1000 * args["loss_ti"]
    best_spar = -1000 * args["loss_spar"] # sparsity loss 

    best_text = ""
    best_text_cs = ""
    best_text_tt = ""
    best_text_spar = ""
    best_text_ti = ""

    regex = re.compile(r"<[|]?end[_]?of[_]?text[|]?>")

    trained_text_embedding = None

    for step in range(opt_iters):
        # randomly sample sample images and get features
        if batch_size is None:
            target_features = all_target_features
        else:
            curr_indx = torch.randperm(len(all_target_features))
            target_features = all_target_features[curr_indx][0:batch_size]
            
        universal_target_features = all_target_features
        
        # forward projection
        projected_embeds, nn_indices = nn_project(prompt_embeds, token_embedding, print_hits=False)

        # get cosine similarity score with all target features
        with torch.no_grad():
            # padded_embeds = copy.deepcopy(dummy_embeds)
            padded_embeds = dummy_embeds.detach().clone()
            padded_embeds[dummy_ids == -1] = projected_embeds.reshape(-1, p_dim)
            logits_per_image, _ = model.forward_text_embedding(model, padded_embeds, dummy_ids, universal_target_features)
            scores_per_prompt = logits_per_image.mean(dim=0)
            universal_cosim_score = scores_per_prompt.max().item()
            best_indx = scores_per_prompt.argmax().item()
        
        # tmp_embeds = copy.deepcopy(prompt_embeds)
        tmp_embeds = prompt_embeds.detach().clone()
        tmp_embeds.data = projected_embeds.data
        tmp_embeds.requires_grad = True
        
        # padding
        # padded_embeds = copy.deepcopy(dummy_embeds)
        padded_embeds = dummy_embeds.detach().clone()
        padded_embeds[dummy_ids == -1] = tmp_embeds.reshape(-1, p_dim)

        # get text-text cosim if original text is provided
        tt_loss = 0
        spar_loss = 0
        if text_target_features is not None:
            combined_embeddings = combine_embeddings(projected_embeds, unpadded_prompt_embeds)
            combined_embeddings.requires_grad=True
            orig_text_embeds = text_target_features
            # text-text loss
            tt_loss = text_text_loss(orig_text_embeds, padded_embeds)
            # text-img loss between orig+trained prompt and image
            ti_loss = text_image_loss(all_target_features, combined_embeddings)
            # sparsity loss
            spar_loss = sparsity_loss(combined_embeddings)
        else:
            tt_loss = 0
            # text-img loss between trained prompt and image
            ti_loss = text_image_loss(all_target_features, projected_embeds)
            # sparsity loss
            spar_loss = sparsity_loss(projected_embeds)
        
        logits_per_image, _ = model.forward_text_embedding(model, padded_embeds, dummy_ids, target_features)
        cosim_scores = logits_per_image

        # loss
        loss =  1 - (cosim_scores.mean() * args["loss_weight"])
        if tt_loss != 0:
            loss += 1 - (tt_loss * args["loss_tt"])
        loss += 1 - (ti_loss * args["loss_ti"])
        loss += (spar_loss * args["loss_spar"])
        
        prompt_embeds.grad, = torch.autograd.grad(loss, [tmp_embeds])
        
        input_optimizer.step()
        input_optimizer.zero_grad()

        curr_lr = input_optimizer.param_groups[0]["lr"]
        cosim_scores = cosim_scores.mean().item()

        decoded_text = decode_ids(nn_indices, tokenizer)[best_indx]
        decoded_text = regex.sub("", decoded_text)

        # we can probably just use the padded embeds
        #trained_text_embedding = get_text_feature(model, tokenizer_funct, device, target_prompts=[decoded_text])

        total_loss = \
              universal_cosim_score * args["loss_weight"] \
            + (tt_loss * args["loss_tt"]) \
            + (ti_loss * args["loss_ti"]) \
            - spar_loss * args["loss_spar"]

        if print_step is not None and (step % print_step == 0 or step == opt_iters-1):
            per_step_message = f"step: {step}, lr: {curr_lr}"
            if not print_new_best:
                per_step_message = f"\n{per_step_message}, cosim: {universal_cosim_score:.3f}, total: {total_loss:.3f}, tt_loss: {tt_loss:.3f}, ti_loss: {ti_loss:.3f}, spar_loss: {spar_loss:.3f}\n text: {decoded_text}"
            print(per_step_message)

        if best_sim * args["loss_weight"] < universal_cosim_score * args["loss_weight"]:
            best_sim = universal_cosim_score * args["loss_weight"]
            best_text_cs = decoded_text
            if print_new_best:
                print(f"new best cosine sim: {best_sim}")
                print(f"new best prompt: {best_text_cs}")

        if best_tt * args["loss_tt"] < tt_loss * args["loss_tt"]:
            best_tt = tt_loss * args["loss_tt"]
            best_text_tt = decoded_text
            if print_new_best:
                print(f"new best tt loss: {best_tt}")
                print(f"new best prompt: {best_text_tt}")

        if best_spar * args["loss_spar"] > spar_loss * args["loss_spar"]:
            best_spar = spar_loss * args["loss_spar"]
            best_text_spar = decoded_text
            if print_new_best:
                print(f"new best tt loss: {best_spar}")
                print(f"new best prompt: {best_text_spar}")

        if best_ti * args["loss_ti"] < ti_loss * args["loss_ti"]:
            best_ti = ti_loss * args["loss_ti"]
            best_text_ti = decoded_text
            if print_new_best:
                print(f"new best tt loss: {best_ti}")
                print(f"new best prompt: {best_text_ti}")

        if best_sum < total_loss:
            best_sum = total_loss
            best_text = decoded_text
            if print_new_best:
                print(f"new best sum: {best_sum}")
                print(f"new best prompt: {best_text}")


    if print_step is not None:
        print()
        print(f"best cosine sim: {best_sim}")
        print(f"best prompt: {best_text}")
        print(f"best prompt cs: {best_text_cs}")
        print(f"best prompt tt: {best_text_tt}")
        print(f"best prompt spar: {best_text_spar}")
        print(f"best prompt ti: {best_text_ti}")

    return best_text

# def optimize_prompt_loop_pez(model, tokenizer, token_embedding, all_target_features, args, device):
#     opt_iters = args["iter"]
#     lr = args["lr"]
#     weight_decay = args["weight_decay"]
#     print_step = args["print_step"]
#     batch_size = args["batch_size"]
#     print_new_best = getattr(args, 'print_new_best', False)

#     # initialize prompt
#     prompt_embeds, dummy_embeds, dummy_ids = initialize_prompt(tokenizer, token_embedding, args, device)
#     p_bs, p_len, p_dim = prompt_embeds.shape

#     # get optimizer
#     input_optimizer = torch.optim.AdamW([prompt_embeds], lr=lr, weight_decay=weight_decay)

#     best_sim = -1000 * args["loss_weight"]
#     best_text = ""

#     for step in range(opt_iters):
#         # randomly sample sample images and get features
#         if batch_size is None:
#             target_features = all_target_features
#         else:
#             curr_indx = torch.randperm(len(all_target_features))
#             target_features = all_target_features[curr_indx][0:batch_size]
            
#         universal_target_features = all_target_features
        
#         # forward projection
#         projected_embeds, nn_indices = nn_project(prompt_embeds, token_embedding, print_hits=False)
#         model

#         # get cosine similarity score with all target features
#         with torch.no_grad():
#             # padded_embeds = copy.deepcopy(dummy_embeds)
#             padded_embeds = dummy_embeds.detach().clone()
#             padded_embeds[dummy_ids == -1] = projected_embeds.reshape(-1, p_dim)
#             logits_per_image, _ = model.forward_text_embedding(model, padded_embeds, dummy_ids, universal_target_features)
#             scores_per_prompt = logits_per_image.mean(dim=0)
#             universal_cosim_score = scores_per_prompt.max().item()
#             best_indx = scores_per_prompt.argmax().item()
        
#         # tmp_embeds = copy.deepcopy(prompt_embeds)
#         tmp_embeds = prompt_embeds.detach().clone()
#         tmp_embeds.data = projected_embeds.data
#         tmp_embeds.requires_grad = True
        
#         # padding
#         # padded_embeds = copy.deepcopy(dummy_embeds)
#         padded_embeds = dummy_embeds.detach().clone()
#         padded_embeds[dummy_ids == -1] = tmp_embeds.reshape(-1, p_dim)
        
#         logits_per_image, _ = model.forward_text_embedding(model, padded_embeds, dummy_ids, target_features)
#         cosim_scores = logits_per_image
#         loss = 1 - cosim_scores.mean()
#         loss = loss * args["loss_weight"]
        
#         prompt_embeds.grad, = torch.autograd.grad(loss, [tmp_embeds])
        
#         input_optimizer.step()
#         input_optimizer.zero_grad()

#         curr_lr = input_optimizer.param_groups[0]["lr"]
#         cosim_scores = cosim_scores.mean().item()

#         decoded_text = decode_ids(nn_indices, tokenizer)[best_indx]
#         if print_step is not None and (step % print_step == 0 or step == opt_iters-1):
#             per_step_message = f"step: {step}, lr: {curr_lr}"
#             if not print_new_best:
#                 per_step_message = f"\n{per_step_message}, cosim: {universal_cosim_score:.3f}, text: {decoded_text}"
#             print(per_step_message)

#         if best_sim * args["loss_weight"] < universal_cosim_score * args["loss_weight"]:
#             best_sim = universal_cosim_score
#             best_text = decoded_text
#             if print_new_best:
#                 print(f"new best cosine sim: {best_sim}")
#                 print(f"new best prompt: {best_text}")


#     if print_step is not None:
#         print()
#         print(f"best cosine sim: {best_sim}")
#         print(f"best prompt: {best_text}")

#     return best_text

def semantic_guidance(text_embeddings, image_features, threshold, dash_token_emb):
    """
    Calculate the semantic guidance loss
    First calculates the word-level similarity to the image features
    Then calculates the masked prompt by substituting tokens with '-' if below threshold.

    Parameters:
    text_embeddings (Tensor): A tensor of shape (batch_size, token_count, d) where d is the embedding dimension.
    image_features (Tensor): A tensor of shape (batch_size, d) where d is the feature dimension.
    threshold (float): The threshold for the sparsity loss.
    """
    loss = 0.0
    token_count = text_embeddings.shape[1]

    og_prompt = text_embeddings
    masked_prompt = text_embeddings.detach().clone()
    # Calculate cosine similarity to image features for each word token
    for i in range(token_count):
        token = text_embeddings[:, i, :]
        token = token / token.norm(dim=-1, keepdim=True)
        similarity = token @ image_features.t()
        # Calculate the masked prompt by substituting tokens with '-' if below threshold
        if similarity < threshold:
            masked_prompt[:, i, :] = dash_token_emb
    return masked_prompt 

def semantic_loss(modified_text_embedding, masked_text_embedding):
    """
    Parameters:
    modified_text_embedding (Tensor): A tensor of shape (batch_size, d) where d is the embedding dimension.
    masked_text_embedding (Tensor): A tensor of shape (batch_size, d) where d is the embedding dimension.
    """
    loss = (modified_text_embedding * masked_text_embedding.t()).sum(dim=-1).mean()
    return loss

# def semantic_loss(text_embeddings, image_features, threshold, dash_token_emb):
#     """
#     Calculate the semantic guidance loss
#     First calculates the word-level similarity to the image features
#     Then calculates the masked prompt by substituting tokens with '-' if below threshold.
# 
#     Parameters:
#     text_embeddings (Tensor): A tensor of shape (batch_size, token_count, d) where d is the embedding dimension.
#     image_features (Tensor): A tensor of shape (batch_size, d) where d is the feature dimension.
#     threshold (float): The threshold for the sparsity loss.
#     """
#     loss = 0.0
#     token_count = text_embeddings.shape[1]
# 
#     og_prompt = text_embeddings
#     masked_prompt = text_embeddings.detach().clone()
#     # Calculate cosine similarity to image features for each word token
#     for i in range(token_count):
#         token = text_embeddings[:, i, :]
#         token = token / token.norm(dim=-1, keepdim=True)
#         similarity = token @ image_features.t()
#         # Calculate the masked prompt by substituting tokens with '-' if below threshold
#         if similarity < threshold:
#             masked_prompt[:, i, :] = dash_token_emb
# 
#     # TODO: fix this
#     og_prompt = og_prompt / og_prompt.norm(dim=-1, keepdim=True)
#     masked_prompt = masked_prompt / masked_prompt.norm(dim=-1, keepdim=True)
#     loss = (og_prompt * masked_prompt).sum(dim=-1).mean()
#     return loss



def sparsity_loss(text_embeddings) -> torch.Tensor:
    """
    Calculate the Sparsity loss more efficiently.

    Parameters:
    text_embeddings (Tensor): A tensor of shape (batch_size, token_count, d) where d is the embedding dimension.

    Returns:
    Tensor: The Sparsity loss value.
    """
    # Normalize the embeddings
    norm_embeddings = text_embeddings / text_embeddings.norm(dim=-1, keepdim=True)

    # Compute all pairwise dot products
    # dot_product.shape = (batch_size, token_count, emb_dim)
    dot_product = torch.matmul(norm_embeddings, norm_embeddings.transpose(-2, -1))

    # Zero out diagonal elements (self dot products) and take absolute values
    batch_size, token_count, emb_dim = text_embeddings.shape
    eye = torch.eye(token_count, device=text_embeddings.device).unsqueeze(0).expand(batch_size, -1, -1)
    dot_product = torch.abs(dot_product) * (1 - eye)

    # Sum over all pairs and average over the batch
    loss = dot_product.sum() / (batch_size * token_count ** 2)
    #loss = dot_product.sum() / (batch_size * token_count * (token_count-1))
    #loss = dot_product.sum() / (batch_size * token_count * emb_dim)

    return loss


def optimize_prompt_s4a(original_prompt: str, img_coarse: list, img_fine: list, target_images=None, target_prompts=None, run_args=None):
    # S4A
    # S4A
    # S4A
    if run_args is None:
        run_args = {
            "prompt_len": 16,
            "iter": 3000,
            "lr": 0.1,
            "weight_decay": 0.1,
            "prompt_bs": 1,
            "print_step": 500,
            "batch_size": 1,
            "print_new_best": False,
            "print_new_best_sum": False,
            "loss_weight": 1.0,
            "loss_qual": 1.0,
            "loss_sem": 1.0,
            "loss_tt": 1.0,
            "loss_ti": 1.0,
            "loss_spar": 1.0,
            "clip_model": "ViT-L/14",
            "pretrained": "openai",
        }

    device = "cuda"
    #device = shared.device
    pretrained='openai'
    clip_model = 'ViT-L/14' # SD XL model is different
    model, _, preprocess = open_clip.create_model_and_transforms(model_name=clip_model, pretrained=pretrained, device=shared.device)
    setattr(model, 'forward_text_embedding', forward_text_embedding)
    setattr(model, 'encode_text_embedding', encode_text_embedding)
        #model, preprocess = clip.load(clip_model, 

    token_embedding = model.token_embedding
    tokenizer = open_clip.tokenizer._tokenizer
    tokenizer_funct = open_clip.tokenizer.tokenize

    # get target features
    img_coarse_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=img_coarse)
    img_fine_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=img_fine)

    # prompt
    #prompt_ids, text_target_features, unpadded_prompt_embeds = get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, run_args, device, target_prompts=[original_prompt])
    # tuple
    #prompt_tensors = get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, run_args, device, target_prompts=[original_prompt])

    if 0:
        all_target_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=img_fine, target_prompts=target_prompts)
            

        prompt_ids, text_target_features, unpadded_prompt_embeds = get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, run_args, device, target_prompts=[original_prompt])
        learned_prompt = optimize_prompt_loop_builtin(model, tokenizer, token_embedding, all_target_features, prompt_ids, text_target_features, unpadded_prompt_embeds, run_args, device, tokenizer_funct)
    else:
        img_coarse_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=img_coarse)
        img_fine_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=img_fine)
        #prompt_tensors = get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, run_args, device, target_prompts=[original_prompt])
        learned_prompt = optimize_prompt_loop_s4a(model, tokenizer, token_embedding, img_coarse_features, img_fine_features, original_prompt, run_args, device, tokenizer_funct)

    return learned_prompt

###


def optimize_prompt(model=None, preprocess=None, args=None, device=None, target_images=None, target_prompts=None):
    global default_args
    m = shared.sd_model
    cond_func = m.get_learned_conditioning
    clip_model = 'ViT-L/14'
    pretrained = 'openai'
    if model == None or preprocess == None:
        print("preprocess is None")
        model, _, preprocess = open_clip.create_model_and_transforms(model_name=clip_model, pretrained="openai", device=device)
        setattr(model, 'forward_text_embedding', forward_text_embedding)
        setattr(model, 'encode_text_embedding', encode_text_embedding)
        #model, preprocess = clip.load(clip_model, 
    run_args = default_args
    if args is not None:
        run_args.update(args)
    token_embedding = model.token_embedding
    tokenizer = open_clip.tokenizer._tokenizer
    tokenizer_funct = open_clip.tokenizer.tokenize

    #m = shared.sd_model
    #model = m.cond_stage_model.tokenizer

    # conditioner = getattr(m, 'conditioner', None)
    # if conditioner is not None:
    #     tokenizer = conditioner.embedders[0]
    #     l_func = lambda text: tokenizer.tokenize(text, None)
    #     tokenizer_funct = l_func
    # else:
    #     tokenizer = m.cond_stage_model.tokenizer
    #     tokenizer_funct = open_clip.tokenizer.tokenize

    #tokenizer_funct = tokenizer.tokenize
    #tokenizer_funct = open_clip.get_tokenizer(clip_model)
    # get target features
    all_target_features = get_target_feature(model, preprocess, tokenizer_funct, device, target_images=target_images, target_prompts=target_prompts)
    prompt_ids, text_target_features, unpadded_prompt_embeds = get_padded_text_feature(model, tokenizer, token_embedding, tokenizer_funct, args, device, target_prompts=target_prompts)

    # optimize prompt
    learned_prompt = optimize_prompt_loop_builtin(model, tokenizer, token_embedding, all_target_features, prompt_ids, text_target_features, unpadded_prompt_embeds, run_args, device, tokenizer_funct)

    return learned_prompt
    

def measure_similarity(orig_images, images, ref_model, ref_clip_preprocess, device):
    with torch.no_grad():
        ori_batch = [ref_clip_preprocess(i).unsqueeze(0) for i in orig_images]
        ori_batch = torch.concatenate(ori_batch).to(device)

        gen_batch = [ref_clip_preprocess(i).unsqueeze(0) for i in images]
        gen_batch = torch.concatenate(gen_batch).to(device)
        
        ori_feat = ref_model.encode_image(ori_batch)
        gen_feat = ref_model.encode_image(gen_batch)
        
        ori_feat = ori_feat / ori_feat.norm(dim=1, keepdim=True)
        gen_feat = gen_feat / gen_feat.norm(dim=1, keepdim=True)
        
        return (ori_feat @ gen_feat.t()).mean().item()

def forward_text_embedding(self, embeddings, ids, image_features, avg_text=False, return_feature=False):
    text_features = self.encode_text_embedding(self, embeddings, ids, avg_text=avg_text)

    if return_feature:
        return text_features

    # normalized features
    image_features = image_features / image_features.norm(dim=1, keepdim=True)
    text_features = text_features / text_features.norm(dim=1, keepdim=True)

    # cosine similarity as logits
    # logit_scale = self.logit_scale.exp()
    logits_per_image = image_features @ text_features.t()
    logits_per_text = logits_per_image.t()

    # shape = [global_batch_size, global_batch_size]
    return logits_per_image, logits_per_text

def encode_text_embedding(self, text_embedding, ids, avg_text=False):
    cast_dtype = self.transformer.get_cast_dtype()

    x = text_embedding + self.positional_embedding.to(cast_dtype)
    x = x.permute(1, 0, 2)  # NLD -> LND
    x = self.transformer(x, attn_mask=self.attn_mask)
    x = x.permute(1, 0, 2)  # LND -> NLD
    x = self.ln_final(x)

    # x.shape = [batch_size, n_ctx, transformer.width]
    # take features from the eot embedding (eot_token is the highest number in each sequence)
    if avg_text:
        x = x[torch.arange(x.shape[0]), :ids.argmax(dim=-1)]
        x[:, 1:-1]
        x = x.mean(dim=1) @ self.text_projection
    else:
        x = x[torch.arange(x.shape[0]), ids.argmax(dim=-1)] @ self.text_projection

    return x

try:
    try:
        if environ["S4A_DEBUG"] == "DEBUG":
            logger = logging.getLogger(__name__) 
            logger.setLevel(logging.DEBUG)
            img_coarse = Image.open("F:\\temp\\s4a\\coarse.png")
            img_fine = Image.open("F:\\temp\\s4a\\fine.png")
            with open("F:\\temp\\s4a\\prompt.txt", mode="r", encoding='ascii') as f:
                prompt = f.readline()
            optimize_prompt_s4a(prompt, [img_coarse], [img_fine])
    except Exception as e:
        logger.exception(e)
        pass

except:
    pass