FROM scan-agent-base:ubuntu24
ARG QWEN_MODEL_REVISION=97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
ENV SCAN_AGENT_EMBEDDING_MODEL_PATH=/opt/scan-agent/models/qwen3-embedding-0.6b
ENV SCAN_AGENT_EMBEDDING_MODEL_REVISION=${QWEN_MODEL_REVISION}
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1
COPY submission/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages --index-url https://download.pytorch.org/whl/cpu \
    torch==2.14.1
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN python3 -c 'import openai, langgraph, pypdf, torch, transformers, sentence_transformers'
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python3 -c \
    "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Qwen/Qwen3-Embedding-0.6B', revision='${QWEN_MODEL_REVISION}', local_dir='/opt/scan-agent/models/qwen3-embedding-0.6b', allow_patterns=['*.json', '*.safetensors', '*.txt', '*.model'])"
RUN rm -rf /submission/*
COPY submission/ /submission/
RUN chmod +x /submission/agent_system
WORKDIR /submission
RUN python3 -m scan_agent.manual_cache
WORKDIR /work
ENTRYPOINT ["/submission/agent_system"]
