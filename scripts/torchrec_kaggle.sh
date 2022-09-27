#!/bin/bash

export DATAPATH=/data/scratch/RecSys/criteo_kaggle_data/
# export DATAPATH=/data/criteo_kaggle_data/
export GPUNUM=1
# local batch size
# 4
# export BATCHSIZE=4096
# 2
# export BATCHSIZE=8192
# 1
export BATCHSIZE=4096

export SHARDTYPE="colossalai"
# export SHARDTYPE="uvm_lfu"

set_n_least_used_CUDA_VISIBLE_DEVICES() {
    local n=${1:-"9999"}
    echo "GPU Memory Usage:"
    local FIRST_N_GPU_IDS=$(nvidia-smi --query-gpu=memory.used --format=csv \
        | tail -n +2 \
        | nl -v 0 \
        | tee /dev/tty \
        | sort -g -k 2 \
        | awk '{print $1}' \
        | head -n $n)
    export CUDA_VISIBLE_DEVICES=$(echo $FIRST_N_GPU_IDS | sed 's/ /,/g')
    echo "Now CUDA_VISIBLE_DEVICES is set to:"
    echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
}
182 
set_n_least_used_CUDA_VISIBLE_DEVICES ${GPUNUM}

# For TorchRec baseline
rm -rf ./tensorboard_log/torchrec_kaggle/w${GPUNUM}_${BATCHSIZE}_${SHARDTYPE}
torchx run -s local_cwd -cfg log_dir=log/torchrec_kaggle/w${GPUNUM}_${BATCHSIZE} dist.ddp -j 1x${GPUNUM} --script baselines/dlrm_main.py -- \
    --in_memory_binary_criteo_path ${DATAPATH} --kaggle --embedding_dim 128 --pin_memory \
    --over_arch_layer_sizes "1024,1024,512,256,1" --dense_arch_layer_sizes "512,256,128" --shuffle_batches \
    --learning_rate 1. --batch_size ${BATCHSIZE} --profile_dir "tensorboard_log/torchrec_kaggle/w${GPUNUM}_${BATCHSIZE}_${SHARDTYPE}" --sharder_type ${SHARDTYPE} --eval_acc 2>&1 | tee logs/torchrec_${GPUNUM}_${BATCHSIZE}_${SHARDTYPE}.txt

# exit(0)
# torchx run -s local_cwd -cfg log_dir=log/torchrec_kaggle/w2_16k dist.ddp -j 1x2 --script baselines/dlrm_main.py -- \
#     --in_memory_binary_criteo_path /data/criteo_kaggle_data --kaggle --embedding_dim 128 --pin_memory \
#     --over_arch_layer_sizes "1024,1024,512,256,1" --dense_arch_layer_sizes "512,256,128" --shuffle_batches \
#     --learning_rate 1. --batch_size 8192  --profile_dir "tensorboard_log/torchrec_kaggle/w2_16k"

# torchx run -s local_cwd -cfg log_dir=log/torchrec_kaggle/w4_16k dist.ddp -j 1x4 --script baselines/dlrm_main.py -- \
#     --in_memory_binary_criteo_path /data/criteo_kaggle_data --kaggle --embedding_dim 128 --pin_memory \
#     --over_arch_layer_sizes "1024,1024,512,256,1" --dense_arch_layer_sizes "512,256,128" --shuffle_batches \
#     --learning_rate 1. --batch_size 4096  --profile_dir "tensorboard_log/torchrec_kaggle/w4_16k"

# torchx run -s local_cwd -cfg log_dir=log/torchrec_kaggle/w8_16k dist.ddp -j 1x8 --script baselines/dlrm_main.py -- \
#     --in_memory_binary_criteo_path /data/criteo_kaggle_data --kaggle --embedding_dim 128 --pin_memory \
#     --over_arch_layer_sizes "1024,1024,512,256,1" --dense_arch_layer_sizes "512,256,128" --shuffle_batches \
#     --learning_rate 1. --batch_size 2048  --profile_dir "tensorboard_log/torchrec_kaggle/w8_16k"