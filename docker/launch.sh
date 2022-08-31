DATASET_PATH=/data/scratch/RecSys

docker run --rm -it -e CUDA_VISIBLE_DEVICES=0,1 -e PYTHONPATH=/workspace/code -v `pwd`:/workspace/code -v ${DATASET_PATH}:/data -w /workspace/code --ipc=host --cap-add SYS_NICE hpcaitech/fawembedding:0.1.0 /bin/bash
