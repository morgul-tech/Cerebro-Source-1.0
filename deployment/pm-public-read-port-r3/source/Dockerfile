FROM python:3.12-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Authentic complete Git objects/history, no build-time repository credential.
COPY canonical-source.bundle /tmp/canonical-source.bundle
RUN git clone --no-checkout /tmp/canonical-source.bundle /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 \
    && git -C /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 checkout --detach b1aafd7cee9385227dd757a64884df2f816296d4 \
    && test "$(git -C /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 rev-parse HEAD)" = "b1aafd7cee9385227dd757a64884df2f816296d4" \
    && git -C /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 diff --exit-code HEAD -- \
    && rm /tmp/canonical-source.bundle \
    && chmod -R a-w /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 \
    && git config --system --add safe.directory /opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4 \
    && chown root:root /etc/gitconfig \
    && chmod 0644 /etc/gitconfig \
    && test "$(git config --system --get-all safe.directory)" = "/opt/cerebro-pm-source/b1aafd7cee9385227dd757a64884df2f816296d4"
RUN useradd --create-home --uid 10001 cerebro

CMD ["python", "-I", "-B", "deployment/assistant_trial_startup.py"]

