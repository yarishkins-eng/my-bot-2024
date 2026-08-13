#!/usr/bin/env bash

# Classify a protected migration recovery from durable state plus live source,
# image and schema evidence.  It never mutates anything.  Unknown or
# contradictory state is a hard stop (exit 64).

set -eu

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

if [ "$#" -ne 8 ]; then
  fail 'usage'
fi

readonly RECOVERY_STATE_FILE="$1"
readonly DEPLOY_STATE_FILE="$2"
readonly TARGET_SHA="$3"
readonly REQUESTED_ROLLBACK_SHA="$4"
readonly REMOTE_MAIN_SHA="$5"
readonly CURRENT_SOURCE_SHA="$6"
readonly CURRENT_IMAGE_ID="$7"
readonly ACTUAL_SCHEMA_REVISIONS="$8"

state_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" || true)"
  [ "$count" = '1' ] || fail "missing_or_duplicate_${key}"
  sed -n "s/^${key}=//p" "$file"
}

optional_state_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" || true)"
  [ "$count" = '0' ] || [ "$count" = '1' ] || fail "duplicate_${key}"
  if [ "$count" = '1' ]; then
    sed -n "s/^${key}=//p" "$file"
  fi
}

valid_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_image() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

valid_revisions() {
  [[ "$1" =~ ^[0-9a-z]+(,[0-9a-z]+)*$ ]]
}

[ -r "$RECOVERY_STATE_FILE" ] || fail 'recovery_state_missing'
[ -r "$DEPLOY_STATE_FILE" ] || fail 'deploy_state_missing'

if grep -Ev '^(format_version|phase|deployed_sha|rollback_source_sha|rollback_image_tag|rollback_image_id|migration_image_id|previous_schema_revisions|target_schema_revisions|old_image_target_schema_compatible)=[^[:space:]]+$' "$RECOVERY_STATE_FILE" >/dev/null; then
  fail 'malformed_recovery_state'
fi

readonly FORMAT_VERSION="$(state_value format_version "$RECOVERY_STATE_FILE")"
readonly RECOVERY_PHASE="$(state_value phase "$RECOVERY_STATE_FILE")"
readonly RECOVERY_DEPLOYED_SHA="$(state_value deployed_sha "$RECOVERY_STATE_FILE")"
readonly RECOVERY_SOURCE_SHA="$(state_value rollback_source_sha "$RECOVERY_STATE_FILE")"
readonly RECOVERY_IMAGE_TAG="$(state_value rollback_image_tag "$RECOVERY_STATE_FILE")"
readonly RECOVERY_IMAGE_ID="$(state_value rollback_image_id "$RECOVERY_STATE_FILE")"
readonly MIGRATION_IMAGE_ID="$(state_value migration_image_id "$RECOVERY_STATE_FILE")"
readonly PREVIOUS_REVISIONS="$(state_value previous_schema_revisions "$RECOVERY_STATE_FILE")"
readonly TARGET_REVISIONS="$(state_value target_schema_revisions "$RECOVERY_STATE_FILE")"
readonly OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE="$(state_value old_image_target_schema_compatible "$RECOVERY_STATE_FILE")"

[ "$FORMAT_VERSION" = '2' ] || fail 'unsupported_recovery_state_version'
[ "$RECOVERY_PHASE" = 'prepared' ] || [ "$RECOVERY_PHASE" = 'completed' ] || fail 'invalid_recovery_phase'
valid_sha "$RECOVERY_DEPLOYED_SHA" || fail 'invalid_deployed_sha'
valid_sha "$RECOVERY_SOURCE_SHA" || fail 'invalid_rollback_source_sha'
[[ "$RECOVERY_IMAGE_TAG" =~ ^teplo-vpn-rollback/bot:pre-migration-[0-9a-f]{40}$ ]] || fail 'invalid_rollback_image_tag'
valid_image "$RECOVERY_IMAGE_ID" || fail 'invalid_rollback_image_id'
valid_image "$MIGRATION_IMAGE_ID" || fail 'invalid_migration_image_id'
valid_revisions "$PREVIOUS_REVISIONS" || fail 'invalid_previous_revisions'
valid_revisions "$TARGET_REVISIONS" || fail 'invalid_target_revisions'
[ "$OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE" = '0' ] || [ "$OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE" = '1' ] || fail 'invalid_compatibility_decision'

valid_sha "$TARGET_SHA" || fail 'invalid_target_sha'
valid_sha "$REQUESTED_ROLLBACK_SHA" || fail 'invalid_requested_rollback_sha'
valid_sha "$REMOTE_MAIN_SHA" || fail 'invalid_remote_main_sha'
valid_sha "$CURRENT_SOURCE_SHA" || fail 'invalid_current_source_sha'
valid_image "$CURRENT_IMAGE_ID" || fail 'invalid_current_image_id'
valid_revisions "$ACTUAL_SCHEMA_REVISIONS" || fail 'invalid_actual_revisions'

[ "$TARGET_SHA" = "$REMOTE_MAIN_SHA" ] || fail 'target_is_not_current_main'
[ "$TARGET_SHA" = "$RECOVERY_DEPLOYED_SHA" ] || fail 'journal_target_mismatch'
[ "$REQUESTED_ROLLBACK_SHA" = "$RECOVERY_SOURCE_SHA" ] || fail 'journal_rollback_source_mismatch'

if [ "$PREVIOUS_REVISIONS" = "$TARGET_REVISIONS" ] && [ "$ACTUAL_SCHEMA_REVISIONS" = "$PREVIOUS_REVISIONS" ]; then
  # A protected database-risk release need not add a new Alembic head. The
  # captured old image was already proven on this unchanged schema, while a
  # completed target state is equally truthful.
  readonly SCHEMA_CLASS='same'
elif [ "$ACTUAL_SCHEMA_REVISIONS" = "$PREVIOUS_REVISIONS" ]; then
  readonly SCHEMA_CLASS='previous'
elif [ "$ACTUAL_SCHEMA_REVISIONS" = "$TARGET_REVISIONS" ]; then
  readonly SCHEMA_CLASS='target'
else
  fail 'unexpected_schema_revision'
fi

if { [ "$SCHEMA_CLASS" = 'target' ] || [ "$SCHEMA_CLASS" = 'same' ]; } && \
  [ "$OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE" != '1' ]; then
  fail 'old_image_not_approved_for_target_schema'
fi

if grep -Ev '^(sha|image|mode|schema_revisions|recovery_from_sha)=[^[:space:]]+$' "$DEPLOY_STATE_FILE" >/dev/null; then
  fail 'malformed_deploy_state'
fi

readonly DEPLOY_SHA="$(state_value sha "$DEPLOY_STATE_FILE")"
readonly DEPLOY_IMAGE="$(state_value image "$DEPLOY_STATE_FILE")"
readonly DEPLOY_MODE="$(optional_state_value mode "$DEPLOY_STATE_FILE")"
valid_sha "$DEPLOY_SHA" || fail 'invalid_deploy_state_sha'
valid_image "$DEPLOY_IMAGE" || fail 'invalid_deploy_state_image'

if [ -z "$DEPLOY_MODE" ]; then
  [ -z "$(optional_state_value schema_revisions "$DEPLOY_STATE_FILE")" ] || fail 'normal_state_has_schema'
  [ -z "$(optional_state_value recovery_from_sha "$DEPLOY_STATE_FILE")" ] || fail 'normal_state_has_recovery_source'
  if [ "$DEPLOY_SHA" = "$RECOVERY_SOURCE_SHA" ] && [ "$DEPLOY_IMAGE" = "$RECOVERY_IMAGE_ID" ]; then
    readonly DEPLOY_CLASS='previous'
  elif [ "$DEPLOY_SHA" = "$TARGET_SHA" ] && [ "$DEPLOY_IMAGE" = "$MIGRATION_IMAGE_ID" ]; then
    readonly DEPLOY_CLASS='target'
  else
    fail 'normal_deploy_state_mismatch'
  fi
elif [ "$DEPLOY_MODE" = 'recovery' ]; then
  readonly DEPLOY_SCHEMA="$(state_value schema_revisions "$DEPLOY_STATE_FILE")"
  readonly DEPLOY_RECOVERY_FROM="$(state_value recovery_from_sha "$DEPLOY_STATE_FILE")"
  [ "$DEPLOY_SHA" = "$RECOVERY_SOURCE_SHA" ] || fail 'recovery_state_source_mismatch'
  [ "$DEPLOY_IMAGE" = "$RECOVERY_IMAGE_ID" ] || fail 'recovery_state_image_mismatch'
  [ "$DEPLOY_SCHEMA" = "$ACTUAL_SCHEMA_REVISIONS" ] || fail 'recovery_state_schema_mismatch'
  [ "$DEPLOY_RECOVERY_FROM" = "$TARGET_SHA" ] || fail 'recovery_state_target_mismatch'
  readonly DEPLOY_CLASS='recovery'
else
  fail 'unknown_deploy_state_mode'
fi

if [ "$DEPLOY_CLASS" = 'target' ] && [ "$SCHEMA_CLASS" != 'target' ] && [ "$SCHEMA_CLASS" != 'same' ]; then
  fail 'target_state_on_previous_schema'
fi

if [ "$RECOVERY_PHASE" = 'completed' ]; then
  [ "$SCHEMA_CLASS" = 'target' ] || [ "$SCHEMA_CLASS" = 'same' ] || fail 'completed_on_previous_schema'
  [ "$DEPLOY_CLASS" = 'target' ] || [ "$DEPLOY_CLASS" = 'recovery' ] || fail 'completed_without_target_state'
fi

[ "$CURRENT_SOURCE_SHA" = "$TARGET_SHA" ] || [ "$CURRENT_SOURCE_SHA" = "$RECOVERY_SOURCE_SHA" ] || fail 'unknown_current_source'
[ "$CURRENT_IMAGE_ID" = "$MIGRATION_IMAGE_ID" ] || [ "$CURRENT_IMAGE_ID" = "$RECOVERY_IMAGE_ID" ] || fail 'unknown_current_image'

if [ "$CURRENT_SOURCE_SHA" = "$RECOVERY_SOURCE_SHA" ] && [ "$CURRENT_IMAGE_ID" = "$MIGRATION_IMAGE_ID" ]; then
  fail 'rollback_source_with_migration_image'
fi

if [ "$DEPLOY_CLASS" = 'recovery' ]; then
  [ "$CURRENT_SOURCE_SHA" = "$RECOVERY_SOURCE_SHA" ] || fail 'recovery_state_live_source_mismatch'
  [ "$CURRENT_IMAGE_ID" = "$RECOVERY_IMAGE_ID" ] || fail 'recovery_state_live_image_mismatch'
fi

if [ "$CURRENT_SOURCE_SHA" = "$RECOVERY_SOURCE_SHA" ] && [ "$CURRENT_IMAGE_ID" = "$RECOVERY_IMAGE_ID" ]; then
  if [ "$DEPLOY_CLASS" = 'recovery' ]; then
    printf 'already_recovered\n'
  else
    printf 'continue_recovery\n'
  fi
elif [ "$CURRENT_SOURCE_SHA" = "$TARGET_SHA" ] && [ "$CURRENT_IMAGE_ID" = "$RECOVERY_IMAGE_ID" ]; then
  printf 'continue_recovery\n'
else
  printf 'start_recovery\n'
fi
