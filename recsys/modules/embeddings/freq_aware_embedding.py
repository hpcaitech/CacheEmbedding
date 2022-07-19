import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from typing import List, Optional


class ChunkCUDAWeightMgr(object):
    """
    Manage Chunk Weights on CPU and CUDA memory.
    CPU maintains a replica of the original weight. CUDA maintains a subset of weight chunks.
    During training, we need to swapin/out chunks.
    """

    def __init__(self, weight: torch.Tensor, chunk_size: int = 16 * 1024 * 1024, cuda_chunk_num: int = 0) -> None:
        self.chunk_size = chunk_size
        self.num_embeddings, self.embedding_dim = weight.shape
        self.cuda_chunk_num = cuda_chunk_num

        device = torch.device('cuda', torch.cuda.current_device())
        self.cuda_partial_weight = torch.empty(cuda_chunk_num * chunk_size, self.embedding_dim, device=device)

        self.chunk_num = (self.num_embeddings + chunk_size - 1) // chunk_size

        # TODO() handle cases where `num_embeddings` is not divisible by chunk_size
        if weight.device.type == 'cuda':
            weight = weight.cpu()
        self.cpu_weight = torch.chunk(weight, self.chunk_num, dim=0)

        # IndexMappingTable: id-> chunk_id, offset_in_chunk
        # a static table build by reorder.
        self.id_to_chunk_ids_mapping = []
        # CachedChunkTable: dict(slot_idx, (chunk_id, offset)) in self.cuda_partial_weight
        self.cached_chunk_table = {}

    def cuda_available_chunk_num(self):
        return self.cuda_chunk_num - len(self.cached_chunk_table)

    def reorder(self, ids_freq_mapping: Optional[List[int]] = None):
        """reorder the cpu_weight according to ids' frequency in dataset before training.
        Also Build the IndexMappingTable, aka id_to_chunk_ids_mapping.
        Args:
            ids_freq_mapping (List[int]): a list, idx is id number, value is freq. if None no reorder
        """
        if ids_freq_mapping is not None:
            sorted_idx = np.flipud(np.argsort(ids_freq_mapping))

        for _id in range(self.num_embeddings):
            self.id_to_chunk_ids_mapping.append(divmod((sorted_idx[_id] if ids_freq_mapping else _id), self.chunk_size))

    def prepare_ids(self, ids: List[int]) -> List[int]:
        """
        move the chunks w.r.t. ids into CUDA memory

        Args:
            ids (List[int]): the ids to be computed
        Returns:
            (List[int]): indices on the cuda_partial_weight.
        """
        chunk_id_set = set()
        for id in ids:
            chunk_id, offset = self.id_to_chunk_ids_mapping[id]
            chunk_id_set.add(chunk_id)

        # move chunk_id_set to CUDA
        cpu_chunk_id_list = []
        for chunk_id in chunk_id:
            if chunk_id not in self.cached_chunk_table:
                cpu_chunk_id_list.append(chunk_id)

        self._prepare_cuda_chunks(self, cpu_chunk_id_list)

    def _prepare_cuda_chunks(self, chunk_ids: List[int]) -> None:
        """prepare chunks in chunk_ids on CUDA memory
        Args:
            chunk_ids (List[int]): the chunks to be placed on CUDA
        """
        pass

    def _evict(self, evit_chunk_num: int):
        """
        evict evit_chunk_num chunks from cuda to cpu.

        Args:
            evit_chunk_num (int): the number of chunks to be evicted
        """
        raise NotImplementedError

    def _find_free_cuda_chunk(self):
        for slot_idx in range(self.cuda_chunk_num):
            if slot_idx not in self.cached_chunk_table:
                return slot_idx
        return -1

    def _admit(self, chunk_id: int):
        """
        move in chunk_id to CUDA

        Args:
            chunk_id (int): the id of chunk to be moved in
        """
        # find a free slot
        slot_id = self._find_free_cuda_chunk()

        if slot_id == -1:
            # evict one chunk
            self._evict(1)

        slot_offset = slot_id * self.chunk_size

        # copy payload from cpu to cuda
        cuda_tensor = torch.narrow(self.cuda_partial_weight, 0, slot_offset,
                                   self.chunk_size * self.embedding_dim).view(self.chunk_size, self.embedding_dim)
        cuda_tensor.copy_(self.cpu_weight[chunk_id])

        # update the CCT
        self.cached_chunk_table[slot_id] = (chunk_id, slot_offset)


class FreqAwareEmbeddingBag(nn.EmbeddingBag):

    def preprocess(self, chunk_size: int, cuda_chunk_num: int, ids_freq_mapping: List[int]):
        """
        Called after initialized. 
        Reorder the weight rows according to the ids_freq_mapping.
        Then, let the weights of the Module be managed by a ChunkCUDAWeightMgr.
        Args:
            chunk_size (int): chunk size
            cuda_chunk_num (int): number of chunk can be hosted in CUDA memory
            ids_freq_mapping (List[int]): a list, idx is id number, value is freq
        """
        self.chunkweightmgr = ChunkCUDAWeightMgr(self.weight, chunk_size, cuda_chunk_num, ids_freq_mapping)

    def forward(self, indices, offsets=None, per_sample_weights=None):
        reorder_ids = self.chunkweightmgr.prepare_ids(indices)

        embeddings = F.embedding_bag(reorder_ids, self.chunkweightmgr.cuda_partial_weight, offsets, self.max_norm,
                                     self.norm_type, self.scale_grad_by_freq, self.mode, self.sparse,
                                     per_sample_weights, self.include_last_offset, self.padding_idx)
        return embeddings
