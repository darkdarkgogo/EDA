FROM scan-agent-base:ubuntu24
COPY submission/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN rm -rf /submission/*
COPY submission/ /submission/
RUN chmod +x /submission/agent_system
WORKDIR /work
ENTRYPOINT ["/submission/agent_system"]
