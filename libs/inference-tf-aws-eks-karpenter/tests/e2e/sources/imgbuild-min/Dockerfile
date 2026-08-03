# Minimal e2e fixture for the image-build job: a base image + a custom wheel from
# the build context, proving arbitrary source dirs (incl. .whl files) build+publish.
# Base pulled via ECR pull-through (public.ecr.aws is a no-creds upstream).
FROM public.ecr.aws/docker/library/python:3.12-slim
COPY dummy_pkg-0.1-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir /tmp/dummy_pkg-0.1-py3-none-any.whl \
    && python -c "import dummy_pkg; assert dummy_pkg.VALUE == 42; print('e2e wheel install OK')"
