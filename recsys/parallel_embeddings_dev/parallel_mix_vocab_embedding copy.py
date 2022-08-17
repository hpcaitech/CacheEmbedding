import math
from functools import partial
from typing import Dict, List, Optional, Union, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch import Tensor
import numpy as np

from recsys import DISTMGR, ParallelMode, DISTLogger
from ..functional import reduce_forward
from .load_balance_mgr import LoadBalanceManager
# from .parallel_mix_vocab_embedding import BlockEmbeddingBag

np.random.seed(111)  
REDUCE_OPS = dict(max=lambda x,dim:torch.max(x,dim=dim)[0], mean=torch.mean, sum=torch.sum)


class BlockEmbeddingBag(nn.Module):

    def __init__(self,
                 num_embeddings: int,
                 block_embedding_dim: int = 64,
                 base_embedding_dim: int = 128,
                 padding_idx: Optional[int] = None,
                 max_norm: Optional[float] = None,
                 norm_type: int = 2.,
                 scale_grad_by_freq: bool = False,
                 sparse: bool = False,
                 mode: str = 'sum',
                 include_last_offset: bool = False,
                 embed_w: Optional[Tensor] = None,
                 linear_w: Optional[Tensor] = None,
                 freeze_w: Optional[bool] = False,
                 device = None,
                 dtype = None,
                 init_method = nn.init.xavier_normal_):
        super().__init__()
        self.num_embeddings = num_embeddings
        self.block_embedding_dim = block_embedding_dim
        self.base_embedding_dim = base_embedding_dim
        self.device = device
        self.dtype = dtype
        
        print('Saved params (M)',(self.num_embeddings*(self.base_embedding_dim-self.block_embedding_dim)\
                                - self.block_embedding_dim*self.base_embedding_dim)/1_000_000)

        if padding_idx is not None:
            if padding_idx > 0:
                assert padding_idx < self.num_embeddings, 'Padding_idx must be within num_embeddings'
            elif padding_idx < 0:
                assert padding_idx >= -self.num_embeddings, 'Padding_idx must be within num_embeddings'
                padding_idx = self.num_embeddings + padding_idx

        self.padding_idx = padding_idx
        self.max_norm = max_norm
        self.norm_type = norm_type
        self.scale_grad_by_freq = scale_grad_by_freq
        self.sparse = sparse
        self.freeze_w = freeze_w

        # Specific to embedding bag
        self.mode = mode
        self.include_last_offset = include_last_offset

        if embed_w is None:
            self.embed_weight = nn.Parameter(
                torch.empty(num_embeddings, block_embedding_dim, device=self.device, dtype=dtype))
            if self.padding_idx is not None:
                with torch.no_grad():
                    self.embed_weight[self.padding_idx].fill_(0)
            init_method(self.embed_weight)
        else:
            assert list(embed_w.shape) == [num_embeddings, block_embedding_dim]
            self.embed_weight = nn.Parameter(embed_w, 
                                             requires_grad=(not self.freeze_w)).to(self.device)

        if block_embedding_dim == base_embedding_dim:
            self.linear_weight = None
        else:
            if linear_w is None:
                self.linear_weight = nn.Parameter(
                    torch.empty(base_embedding_dim, block_embedding_dim, device=self.device, dtype=dtype))
                init_method(self.linear_weight)
            else:
                assert list(linear_w.shape) == [base_embedding_dim, block_embedding_dim], \
                    "Pretrained weights have dimension {x1}, which is different from linear layer dimensions {x2} \
                        ".format(x1=list(linear_w.shape), x2=[block_embedding_dim, base_embedding_dim])
                self.linear_weight = nn.Parameter(linear_w, 
                                                  requires_grad=(not self.freeze_w)).to(self.device)

    def forward(self, input_: Tensor, offsets=None, per_sample_weights=None):
        input_, nonzero_counts_ = self._handle_unexpected_inputs(input_)
        output_parallel = F.embedding_bag(input_, self.embed_weight, offsets, self.max_norm, self.norm_type,
                                        self.scale_grad_by_freq, self.mode, self.sparse, per_sample_weights,
                                        self.include_last_offset, self.padding_idx)

        if self.block_embedding_dim != self.base_embedding_dim:
            output_parallel = F.linear(output_parallel, self.linear_weight, bias=None)
        assert output_parallel.size() == (input_.size(0), self.base_embedding_dim)
        return output_parallel
    
    def _handle_unexpected_inputs(self, input_: Tensor) -> Tensor:
        nonzero_counts_ = None
        if torch.max(input_) > self.num_embeddings or torch.min(input_) < 0:
            if self.padding_idx is None:
                self.padding_idx = self.num_embeddings
                _embed_weight = torch.empty((self.num_embeddings+1, self.block_embedding_dim),
                                            device=self.device, dtype=self.dtype)
                _padding_weight = torch.zeros((1, self.block_embedding_dim),
                                            device=self.device, dtype=self.dtype)
                _embed_weight.data.copy_(torch.cat([self.embed_weight.data,
                                                   _padding_weight.data],
                                                   dim=0))
                self.embed_weight = nn.Parameter(_embed_weight)
            with torch.no_grad():
                self.embed_weight[self.padding_idx].fill_(-float('inf') 
                                                            if self.mode == 'max' else 0)
            input_[(input_ >= self.num_embeddings) | (input_ < 0)] = self.padding_idx
        return input_, nonzero_counts_

    @classmethod
    def from_pretrained(cls,
                        weights: List[Tensor],
                        base_embedding_dim: int = 128,
                        freeze: bool = False,
                        max_norm: Optional[float] = None,
                        norm_type: float = 2.,
                        scale_grad_by_freq: bool = False,
                        mode: str = 'sum',
                        sparse: bool = False,
                        include_last_offset: bool = False,
                        padding_idx: Optional[int] = None,
                        device = None,
                        dtype = None,
                        init_method = nn.init.xavier_normal_) -> 'BlockEmbeddingBag':
        assert len(weights) == 2 and weights[0].dim() == 2, \
            'Both embedding and linear weights are expected \n \
            Embedding parameters are expected to be 2-dimensional'
        rows, cols = weights[0].shape
        embeddingbag = cls(num_embeddings=rows,
                           block_embedding_dim=cols,
                           base_embedding_dim=base_embedding_dim,
                           embed_w=weights[0],
                           linear_w=weights[1],
                           freeze_w=freeze,
                           max_norm=max_norm,
                           norm_type=norm_type,
                           scale_grad_by_freq=scale_grad_by_freq,
                           mode=mode,
                           sparse=sparse,
                           include_last_offset=include_last_offset,
                           padding_idx=padding_idx,
                           device=device,
                           dtype=dtype,
                           init_method=init_method)
        return embeddingbag

    def get_base_embedding_dim(self) -> int:
        return self.base_embedding_dim

    def get_weights(self, detach: bool = False) -> List[Optional[Tensor]]:
        assert isinstance(self.embed_weight, Tensor)
        if detach and self.padding_idx is not None and \
            self.padding_idx == self.num_embeddings:
            self.embed_weight = nn.Parameter(self.embed_weight[:self.padding_idx,:],
                                             requires_grad=(not self.freeze_w))
        if self.linear_weight is None:
            return [self.embed_weight.detach() if detach 
                    else self.embed_weight, None]
        else:
            return [self.embed_weight.detach() if detach 
                    else self.embed_weight, self.linear_weight.detach() if detach else self.linear_weight]


def determine_freq_blocks(word_frequencies: Tensor,
                          num_embeddings: int,
                          num_blocks: int,
                          base_embedding_dim: int) \
    -> Tuple[List[int], Dict, List[int]]:
    assert word_frequencies.dim()==1 and len(word_frequencies) == num_embeddings
    dim_indices = np.argsort(word_frequencies.numpy())
    sort_word_freqs = np.sort(word_frequencies.numpy())
    tile_len = num_embeddings // num_blocks
    total_sum = sum(word_frequencies)
    block_embedding_dims = []
    num_embeddings_per_block = []
    id_group_map = {} # feature-id -> (group-id, in-group-position)
    
    def compute_block_dim(quotient):
        return max(2, int(base_embedding_dim / 2**(int(math.log2(quotient)))))

    for i in range(num_blocks):
        if i == num_blocks-1:
            local_sum = sum(sort_word_freqs[i*tile_len:])
            indices = dim_indices[i*tile_len:]
        else:
            local_sum = sort_word_freqs[i*tile_len:(i+1)*tile_len]
            indices = dim_indices[i*tile_len:(i+1)*tile_len]
        block_dim = compute_block_dim(total_sum / local_sum)
        in_group_index = 0
        for j in indices:
            id_group_map[j] = (i, in_group_index)
            in_group_index += 1
        block_embedding_dims[i] = block_dim
        num_embeddings_per_block[i] = len(indices)
        
    return num_embeddings_per_block, id_group_map, block_embedding_dims

class MultiBlockEmbeddingBag(nn.Module):
    def __init__(self,
                 word_frequencies: Tensor,
                 num_embeddings: int,
                 num_blocks: int = 4,
                 base_embedding_dim: int = 128,
                 mode: str = 'sum', 
                 device = None,
                 *args,
                 **kwargs):
        super().__init__()
        self.num_blocks = num_blocks
        self.num_embeddings_per_block , self.id_group_map, self.block_embedding_dims = \
                determine_freq_blocks(word_frequencies,num_embeddings,self.num_blocks,base_embedding_dim)
        self.base_embedding_dim = base_embedding_dim
        self._sanity_check()
        self.block_embeds = [BlockEmbeddingBag(
                                self.num_embeddings_per_block[i],
                                self.block_embedding_dims[i],
                                self.base_embedding_dim,
                                device=device,
                                mode=mode,
                                *args,
                                **kwargs) 
                             for i in range(self.num_blocks)]
        self.mode = mode
        self.device = device
        
    def _sanity_check(self):
        assert self.num_blocks>=1 and self.num_blocks == len(self.num_embeddings_per_block)
        assert self.num_blocks == len(self.block_embedding_dims)
        assert self.base_embedding_dim >= max(self.block_embedding_dims)
        
    def forward(self, input_: Tensor, offsets=None, per_sample_weights=None):
        assert input_.dim() == 2
        outputs = []
        for i in range(self.num_blocks):
            outputs.append(self.block_embeds[i](input_, offsets, per_sample_weights).unsqueeze(0))
            
        return REDUCE_OPS[self.mode](torch.cat(outputs,dim=0),dim=1)


class ParallelBlockEmbeddingBag(nn.Module):

    def __init__(self,
                embeddings_per_feat: List[int],
                embedding_dim: int = 128,
                parallel_mode: Optional[ParallelMode] = None,
                padding_idx: Optional[int] = None,
                max_norm: Optional[float] = None,
                norm_type: int = 2.,
                scale_grad_by_freq: bool = False,
                sparse: bool = False,
                mode: str = 'sum',
                do_fair: bool = True,
                include_last_offset: bool = False,
                lbmgr: Optional[LoadBalanceManager] = None,
                embed_w: Optional[Tensor] = None,
                linear_w: Optional[Tensor] = None,
                freeze_w: Optional[bool] = False,
                device = None,
                dtype = None,
                init_method = nn.init.xavier_normal_):
        super().__init__()
        self.embeddings_per_feat = embeddings_per_feat
        self.embedding_dim = embedding_dim
        self.mode = mode
        self.device = device if device is not None else torch.device('cuda', torch.cuda.current_device())
        self.dtype = dtype

        # distributed mode setting
        self.parallel_mode = ParallelMode.DEFAULT if parallel_mode is None else parallel_mode
        self.world_size = DISTMGR.get_world_size(self.parallel_mode)
        self.rank = DISTMGR.get_rank(self.parallel_mode)
        self.num_groups = self.world_size # default setting

        if lbmgr is not None:
            self.lbmgr = lbmgr
            self.embeddings_per_feat = lbmgr.get_embeddings_per_feat()
            self.embedding_dim = lbmgr.get_base_dim()
        else:
            self.lbmgr = LoadBalanceManager(embeddings_per_feat, self.num_groups,\
                embedding_dim, do_fair=True, device=self.device)

        self.num_embeddings_on_rank = self.lbmgr.get_num_embeddings_on_rank(self.rank)
        self.block_dim = self.lbmgr.get_block_dim(self.rank)

        # specific to embedding bag
        self.comm_func = reduce_forward
        self.padding_idx = padding_idx
        self.max_norm = max_norm
        self.norm_type = norm_type
        self.scale_grad_by_freq = scale_grad_by_freq
        self.include_last_offset = include_last_offset
        self.sparse = sparse

        if embed_w is None:
            self.embed_weight = nn.Parameter(
                    torch.empty(self.num_embeddings_on_rank, 
                                self.block_dim, device=self.device, dtype=self.dtype))
            if self.padding_idx is not None:
                with torch.no_grad():
                    self.embed_weight[self.padding_idx].fill_(0)
            init_method(self.embed_weight)
        else:
            assert list(embed_w.shape) == [self.num_embeddings_on_rank, self.block_dim]
            self.embed_weight = nn.Parameter(embed_w, 
                                             requires_grad=(not freeze_w)).to(self.device)

        if self.block_dim == self.embedding_dim:
            self.linear_weight = None
        else:
            if linear_w is None:
                linear_w = nn.Parameter(
                    torch.empty(self.embedding_dim, self.block_dim, device=self.device, dtype=dtype))
                init_method(linear_w)
                dist.broadcast(linear_w, src=0)
                self.linear_weight = linear_w
            else:
                assert list(linear_w.shape) == [self.embedding_dim, self.block_dim], \
                    "Pretrained weights have dimension {x1}, which is different from linear layer dimensions {x2} \
                        ".format(x1=list(linear_w.shape), x2=[self.block_dim, self.embedding_dim])
                self.linear_weight = nn.Parameter(linear_w, 
                                                  requires_grad=(not freeze_w)).to(self.device)
    
    def forward(self, input_: Tensor, offsets=None, per_sample_weights=None):
        input_ = self.lbmgr.shard_tensor(input_, self.rank)
        input_ = self._handle_unexpected_inputs(input_)
        output_parallel = F.embedding_bag(input_, self.embed_weight, offsets, self.max_norm, self.norm_type,
                                        self.scale_grad_by_freq, self.mode, self.sparse, per_sample_weights,
                                        self.include_last_offset, self.padding_idx)
        output_gather = self.comm_func(output_parallel, self.parallel_mode, reduce_op=self.mode)
        
        if self.block_embedding_dim != self.embedding_dim:
            output_gather = F.linear(output_gather, self.linear_weight, bias=None)
        assert output_gather.size() == (input_.size(0), self.embedding_dim)
        return output_gather
    
    def _handle_unexpected_inputs(self, input_: Tensor) -> Tensor:
        if torch.max(input_) > self.num_embeddings or torch.min(input_) < 0:
            if self.padding_idx is None:
                self.padding_idx = self.num_embeddings
                _embed_weight = torch.empty((self.num_embeddings+1, self.block_embedding_dim),
                                            device=self.device, dtype=self.dtype)
                _padding_weight = torch.zeros((1, self.block_embedding_dim),
                                            device=self.device, dtype=self.dtype)
                _embed_weight.data.copy_(torch.cat([self.embed_weight.data,
                                                   _padding_weight.data],
                                                   dim=0))
                self.embed_weight = nn.Parameter(_embed_weight)
            with torch.no_grad():
                self.embed_weight[self.padding_idx].fill_(-float('inf') 
                                                            if self.mode == 'max' else 0)
            input_[(input_ >= self.num_embeddings) | (input_ < 0)] = self.padding_idx
        return input_

    @classmethod
    def from_pretrained(cls,
                        weights: List[Tensor],
                        base_embedding_dim: int = 128,
                        freeze: bool = False,
                        max_norm: Optional[float] = None,
                        norm_type: float = 2.,
                        scale_grad_by_freq: bool = False,
                        mode: str = 'sum',
                        sparse: bool = False,
                        include_last_offset: bool = False,
                        padding_idx: Optional[int] = None,
                        device = None,
                        dtype = None,
                        init_method = nn.init.xavier_normal_) -> 'BlockEmbeddingBag':
        assert len(weights) == 2 and weights[0].dim() == 2, \
            'Both embedding and linear weights are expected \n \
            Embedding parameters are expected to be 2-dimensional'
        rows, cols = weights[0].shape
        embeddingbag = cls(num_embeddings=rows,
                           block_embedding_dim=cols,
                           base_embedding_dim=base_embedding_dim,
                           embed_w=weights[0],
                           linear_w=weights[1],
                           freeze_w=freeze,
                           max_norm=max_norm,
                           norm_type=norm_type,
                           scale_grad_by_freq=scale_grad_by_freq,
                           mode=mode,
                           sparse=sparse,
                           include_last_offset=include_last_offset,
                           padding_idx=padding_idx,
                           device=device,
                           dtype=dtype,
                           init_method=init_method)
        return embeddingbag

    def get_weights(self, detach: bool = False) -> List[Optional[Tensor]]:
        assert isinstance(self.embed_weight, Tensor)
        if detach and self.padding_idx is not None and \
            self.padding_idx == self.num_embeddings:
            self.embed_weight = nn.Parameter(self.embed_weight[:self.padding_idx,:],
                                             requires_grad=(not self.freeze_w))
        if self.linear_weight is None:
            return [self.embed_weight.detach() if detach 
                    else self.embed_weight, None]
        else:
            return [self.embed_weight.detach() if detach 
                    else self.embed_weight, self.linear_weight.detach() if detach else self.linear_weight]
