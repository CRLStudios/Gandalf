#!/bin/sh
# GIT_ASKPASS helper — supplies credentials from env without leaking them
# into process args or .git/config.
case "$1" in
  [Uu]sername*) printf '%s\n' "${GIT_ASKPASS_USERNAME:-x-access-token}" ;;
  *) printf '%s\n' "${GIT_ASKPASS_PASSWORD:-}" ;;
esac
