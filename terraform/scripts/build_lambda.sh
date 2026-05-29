#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: build_lambda.sh <source_dir> <build_dir> <requirements_file>" >&2
  exit 64
fi

SOURCE_DIR=$1
BUILD_DIR=$2
REQUIREMENTS_FILE=$3

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

python3 -m pip install \
  --disable-pip-version-check \
  --quiet \
  --requirement "$REQUIREMENTS_FILE" \
  --target "$BUILD_DIR" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade

cp -R "$SOURCE_DIR"/. "$BUILD_DIR"/

find "$BUILD_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$BUILD_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
