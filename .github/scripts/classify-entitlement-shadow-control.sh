#!/usr/bin/env bash

# Pure read-only classifier for the protected Gate 2 runtime switch. It binds
# workflow source, current main, deployed source/image and requested action to
# one explicit decision before any container change.

set -eu

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

[ "$#" -eq 7 ] || fail 'usage'

readonly ACTION="$1"
readonly WORKFLOW_SHA="$2"
readonly EXPECTED_DEPLOYED_SHA="$3"
readonly CURRENT_MAIN_SHA="$4"
readonly DEPLOYED_SHA="$5"
readonly DEPLOY_STATE_IMAGE="$6"
readonly LIVE_IMAGE="$7"

valid_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_image() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

case "$ACTION" in
  ENABLE_SHADOW|DISABLE_SHADOW) ;;
  *) fail 'action_not_allowlisted' ;;
esac
valid_sha "$WORKFLOW_SHA" || fail 'invalid_workflow_sha'
valid_sha "$EXPECTED_DEPLOYED_SHA" || fail 'invalid_expected_deployed_sha'
valid_sha "$CURRENT_MAIN_SHA" || fail 'invalid_current_main_sha'
valid_sha "$DEPLOYED_SHA" || fail 'invalid_deployed_sha'
valid_image "$DEPLOY_STATE_IMAGE" || fail 'invalid_deploy_state_image'
valid_image "$LIVE_IMAGE" || fail 'invalid_live_image'
[ "$DEPLOYED_SHA" = "$EXPECTED_DEPLOYED_SHA" ] || fail 'owner_expected_deployed_sha_mismatch'
[ "$DEPLOY_STATE_IMAGE" = "$LIVE_IMAGE" ] || fail 'deploy_state_image_mismatch'

if [ "$ACTION" = 'ENABLE_SHADOW' ]; then
  [ "$WORKFLOW_SHA" = "$CURRENT_MAIN_SHA" ] || fail 'workflow_is_not_current_main'
  [ "$DEPLOYED_SHA" = "$WORKFLOW_SHA" ] || fail 'enable_requires_exact_main'
  printf 'enable_exact\n'
else
  printf 'disable_compatible_check_required\n'
fi
