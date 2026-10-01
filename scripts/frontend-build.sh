#!/bin/sh
# This recipe is a frontend revision input; validation/packaging stay separate.
set -eu
exec npm run build-prod
