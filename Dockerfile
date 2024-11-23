FROM python:3.11-slim AS build

# Install build dependencies
RUN pip install uv

# Copy application code
WORKDIR /app
COPY . /app

# Install the package
RUN pip install .
