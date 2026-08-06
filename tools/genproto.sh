#!/usr/bin/env bash
# Regenerate the Python protobuf bindings from protos/*.proto.
#
# The generated *_pb2.py files are not committed: they are tied to the
# protobuf runtime version, so packaging regenerates them at build time
# against whatever python3-protobuf the target distribution ships.
#
# Usage: tools/genproto.sh [python-interpreter]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${1:-python3}"

cd "$here"
mkdir -p nearshare/proto
"$python" -m grpc_tools.protoc -Iprotos --python_out=nearshare/proto \
    protos/*.proto

# protoc emits absolute imports ("import securemessage_pb2"); rewrite them
# to package-relative so the modules import correctly from nearshare.proto.
sed -i 's/^import \([a-z_]*_pb2\) as/from . import \1 as/' \
    nearshare/proto/*_pb2.py

touch nearshare/proto/__init__.py
echo "generated $(ls nearshare/proto/*_pb2.py | wc -l) protobuf modules"

# NOTE ON TARGETS -- the generated code is NOT interchangeable:
#
#   tools/genproto.sh .venv/bin/python   dev, tests and the snap (which
#                                        pip-installs a modern protobuf)
#   tools/genproto.sh python3            the .deb: Ubuntu's own protoc,
#                                        matching the python3-protobuf
#                                        the package depends on
#
# Modern protoc emits a runtime-version guard that Ubuntu's protobuf
# rejects, and Ubuntu's protoc emits pre-4.x descriptors that the PyPI
# protobuf rejects. Regenerate for the target you are about to build;
# `debuild` in particular needs the python3 form (see debian/rules).
