FROM scan-agent-base:ubuntu24
COPY submission/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN python3 -c 'import openai, langgraph, pypdf'
RUN rm -rf /submission/*
COPY submission/ /submission/
RUN chmod +x /submission/agent_system
WORKDIR /submission
WORKDIR /work
ENTRYPOINT ["/submission/agent_system"]
