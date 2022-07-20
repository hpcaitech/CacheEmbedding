import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from typing import List, Optional
from contexttimer import Timer

from .base_embeddings import BaseEmbeddingBag


class ChunkCUDAWeightMgr(object):
    """
    Manage Chunk Weights on CPU and CUDA memory.
    CPU maintains a replica of the original weight. CUDA maintains a subset of weight chunks.
    During training, we need to swapin/out chunks.
    """

    def __init__(self,
                 weight: torch.Tensor,
                 chunk_size: int = 16 * 1024 * 1024,
                 cuda_chunk_num: int = 0,
                 *args,
                 **kwargs) -> None:
        self.chunk_size = chunk_size
        self.num_embeddings, self.embedding_dim = weight.shape
        self.cuda_chunk_num = cuda_chunk_num

        self.elem_size_in_byte = weight.element_size()

        self.cuda_partial_weight = torch.nn.Parameter(
            torch.empty(cuda_chunk_num * chunk_size * self.embedding_dim, device=torch.cuda.current_device()))

        self.chunk_num = (self.num_embeddings + chunk_size - 1) // chunk_size

        if weight.device.type == 'cuda':
            weight = weight.cpu()

        # handle cases where `num_embeddings` is not divisible by chunk_size
        mod = weight.shape[0] % chunk_size
        if mod > 0:
            with torch.no_grad():
                padding = torch.zeros(chunk_size - mod, weight.shape[1], device=weight.device, dtype=weight.dtype)
                weight = torch.cat([weight, padding], dim=0)
        self.cpu_weight = torch.chunk(weight.detach(), self.chunk_num, dim=0)

        self.cpu_weight = [t.pin_memory() for t in self.cpu_weight]

        # IndexMappingTable: id-> chunk_id, offset_in_chunk
        # a static table build by reorder.
        self.index_mapping_table = []

        # CachedChunkTable: dict(slot_idx, (chunk_id, offset)) in self.cuda_partial_weight
        self.cached_chunk_table = {}
        # chunk_id, offset in cuda_partial_weight
        self.chunk_id_cuda_offset = {}
        self.evict_backlist = set()

        self._cpu_to_cuda_numel = 0
        self._cpu_to_cuda_elpase = 0
        self._cuda_to_cpu_numel = 0
        self._cuda_to_cpu_elapse = 0

    def _reset_comm_stats(self):
        self._cpu_to_cuda_numel = 0
        self._cuda_to_cpu_numel = 0

    def _chunk_in_cuda(self, chunk_id) -> bool:
        for slot_idx, (_chunk_id, offset) in self.cached_chunk_table.items():
            if _chunk_id == chunk_id:
                return True
        return False

    def cuda_available_chunk_num(self):
        return self.cuda_chunk_num - len(self.cached_chunk_table)

    def reorder(self, ids_freq_mapping: Optional[List[int]] = None):
        """reorder the cpu_weight according to ids' frequency in dataset before training.
        Also Build the IndexMappingTable, aka index_mapping_table.
        Args:
            ids_freq_mapping (List[int]): a list, idx is id number, value is freq. if None no reorder
        """
        if ids_freq_mapping is not None:
            sorted_idx = np.flipud(np.argsort(ids_freq_mapping))

        for _id in range(self.num_embeddings):
            self.index_mapping_table.append(divmod((sorted_idx[_id] if ids_freq_mapping else _id), self.chunk_size))

        print(self.index_mapping_table)
    
    def _id_to_cached_cuda_id(self, id: int) -> int:
        """
        convert an id from the dataset to index in self.partial_cuda_weight

        Args:
            id (int): an id from the dataset

        Returns:
            int: the index in self.partial_cuda_weight
        """
        chunk_id, offset_in_chunk = self.index_mapping_table[id]
        return int(self.chunk_id_cuda_offset[chunk_id] + offset_in_chunk)

    def prepare_ids(self, ids: torch.Tensor) -> torch.Tensor:
        """
        move the chunks w.r.t. ids into CUDA memory

        Args:
            ids (torch.Tensor): the ids to be computed
        Returns:
            torch.Tensor: indices on the cuda_partial_weight.
        """
        chunk_id_set = set()
        for id in ids:
            chunk_id, offset_in_chunk = self.index_mapping_table[id]
            chunk_id_set.add(chunk_id)

        self.evict_backlist = chunk_id_set

        # move chunk_id_set to CUDA
        cpu_chunk_id_list = []
        for chunk_id in chunk_id_set:
            if not self._chunk_in_cuda(chunk_id):
                cpu_chunk_id_list.append(chunk_id)

        # move sure the cuda chunk will not be evicted!

        self._prepare_cuda_chunks(cpu_chunk_id_list)

        self.evict_backlist
        # new ids chunk_offset + offset_in_chunk
        mapped_ids = [self._id_to_cached_cuda_id(id) for id in ids.view(-1)]
        return torch.IntTensor(mapped_ids).to(ids.device).view(ids.shape)

    def _prepare_cuda_chunks(self, chunk_ids: List[int]) -> None:
        """prepare chunks in chunk_ids on CUDA memory
        Args:
            chunk_ids (List[int]): the chunks to be placed on CUDA
        """
        for chunk_id in chunk_ids:
            if self._chunk_in_cuda(chunk_id):
                continue

            if self._find_free_cuda_slot() == -1:
                self._evict()
            self._admit(chunk_id)

    def _evict(self):
        """
        evict one chunk from cuda to cpu.
        """
        min_chunk_id = 2 * self.chunk_num
        min_slot_id = None
        min_offset = None
        for slot_id, (chunk_id, offset) in self.cached_chunk_table.items():
            if chunk_id < min_chunk_id and chunk_id not in self.evict_backlist:
                min_chunk_id = chunk_id
                min_slot_id = slot_id
                min_offset = offset

        if min_slot_id is None:
            raise RuntimeError("Can not evict a chunk")
        
        with Timer() as timer:
            cuda_tensor = torch.narrow(self.cuda_partial_weight, 0, min_offset,
                                       self.chunk_size * self.embedding_dim).view(self.chunk_size, self.embedding_dim)
            self.cpu_weight[min_chunk_id].data.copy_(cuda_tensor)

        # update CCT
        self.cached_chunk_table.pop(min_slot_id)
        self.chunk_id_cuda_offset.pop(min_chunk_id)

        self._cuda_to_cpu_numel += self.chunk_size * self.embedding_dim
        self._cuda_to_cpu_elapse += timer.elapsed

    def _find_free_cuda_slot(self) -> int:
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
        slot_id = self._find_free_cuda_slot()

        if slot_id == -1:
            # evict one chunk
            self._evict()

        slot_offset = slot_id * self.chunk_size

        # copy payload from cpu to cuda
        with Timer() as timer:
            cuda_tensor = torch.narrow(self.cuda_partial_weight, 0, slot_offset,
                                       self.chunk_size * self.embedding_dim).view(self.chunk_size, self.embedding_dim)
            cuda_tensor.data.copy_(self.cpu_weight[chunk_id])

        # update the CCT
        self.cached_chunk_table[slot_id] = (chunk_id, slot_offset)
        self.chunk_id_cuda_offset[chunk_id] = slot_offset

        self._cpu_to_cuda_numel += self.chunk_size * self.embedding_dim
        self._cpu_to_cuda_elpase += timer.elapsed

    def flush(self):
        """flush all CUDA chunks to CPU.
        The function is usually called after training finished.
        """
        while len(self.cached_chunk_table) != 0:
            self._evict()

    def print_comm_stats(self):
        if self._cuda_to_cpu_numel > 0:
            print(
                f"CUDA->CPU BWD {self._cuda_to_cpu_numel * self.elem_size_in_byte / 1e6 / self._cuda_to_cpu_elapse} MB/s {self._cuda_to_cpu_numel / 1e6} M elem"
            )
        if self._cpu_to_cuda_numel > 0:
            print(
                f"CPU->CUDA BWD {self._cpu_to_cuda_numel * self.elem_size_in_byte / 1e6 / self._cpu_to_cuda_elpase} MB/s {self._cpu_to_cuda_numel / 1e6} M elem"
            )


class FreqAwareEmbeddingBag(BaseEmbeddingBag):

    def __init__(self,
                 num_embeddings,
                 embedding_dim,
                 dtype=None,
                 *args,
                 **kwargs):
        super(FreqAwareEmbeddingBag, self).__init__(num_embeddings, embedding_dim, *args, **kwargs)
        self._weight = torch.randn(self.num_embeddings, self.embedding_dim, device='cpu', dtype=dtype)

    def _preprocess(self, chunk_size: int, cuda_chunk_num: int, ids_freq_mapping: Optional[List[int]] = None):
        """
        Called after initialized. 
        Reorder the weight rows according to the ids_freq_mapping.
        Then, let the weights of the Module be managed by a ChunkCUDAWeightMgr.
        Args:
            chunk_size (int): chunk size
            cuda_chunk_num (int): number of chunk can be hosted in CUDA memory
            ids_freq_mapping (List[int]): a list, idx is id number, value is freq
        """
        self.chunk_weight_mgr = ChunkCUDAWeightMgr(self._weight, chunk_size, cuda_chunk_num, ids_freq_mapping)
        self.chunk_weight_mgr.reorder(ids_freq_mapping)

    def forward(self, indices, offsets=None, per_sample_weights=None):
        print(indices, indices.shape)
        reorder_ids = self.chunk_weight_mgr.prepare_ids(indices)
        print(reorder_ids)
        embeddings = F.embedding_bag(reorder_ids, self.chunk_weight_mgr.cuda_partial_weight, offsets, self.max_norm,
                                     self.norm_type, self.scale_grad_by_freq, self.mode, self.sparse,
                                     per_sample_weights, self.include_last_offset, self.padding_idx)

        return embeddings

    @property
    def weight(self):
        return self.chunk_weight_mgr.cpu_weight
