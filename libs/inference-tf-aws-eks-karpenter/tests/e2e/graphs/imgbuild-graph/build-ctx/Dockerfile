# Minimal build context for the onboarder `builds:` e2e: a base image + a trivial layer.
# Base pulled via ECR pull-through (public.ecr.aws is a no-creds upstream); mirrors the
# image-build fixture (sources/imgbuild-min). The onboarder builds this into
# <ecr>/workload/imgbuild-graph-app:v1 and rewrites the graph to its @digest.
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN python -c "print('onboarder builds: e2e image OK')"
