#!/bin/bash
set -euo pipefail
cd $(dirname "$0")

CHART_NEEDS_VERSION_UP_FILE=Chart.yaml.needs_version_up
trap "rm -f $CHART_NEEDS_VERSION_UP_FILE &> /dev/null" EXIT

function next_chart_version() {
  CURR=$(yq '.version' Chart.yaml)
  NEXT=$(yq -p yaml -o json '.dependencies' Chart.yaml | jq -r '.[] | select(.name == "crowdsec") | .version')

  HIGHEST=$(semver "$CURR" "$NEXT-0" | tail -n1)

  if [ "$HIGHEST" == "$CURR" ] ; then
    if [[ $CURR =~ - ]] ; then
      PRE_REL=${CURR##*-}
    else
      PRE_REL=0
    fi
    semver -i prerelease ${CURR%%-*}-$PRE_REL
  else
    echo "$NEXT-0"
  fi
}

function yq_update_value_inplace() {
  KEY=${1:?Missing KEY as #1 argument}
  VALUE=${2:?Missing VALUE as #2 argument}
  FILE=${3:?Missing FILE as #3 argument}
  KEY_POSITION=$(yq "$KEY | line" "$FILE")
  sed -e "${KEY_POSITION}s#${KEY##*.}: .*#${KEY##*.}: $VALUE#" -i "$FILE"
}

yq .dependencies -p yaml -o json Chart.yaml | jq -r 'to_entries[] | [(.key|tostring), .value.name, .value.version, .value.repository, .value.alias] | join(" ")' | while read INDEX NAME VERSION REPO_URL ALIAS _ ; do
  echo "** Updating subchart $NAME to latest"

  if [[ "$REPO_URL" =~ ^oci:// ]] ; then
    CHART_INFO=$(helm show chart "$REPO_URL/$NAME")
  else
    REPO_URL=$(sed -e 's#/*$##' <<<"$REPO_URL")
    REPO_NAME=$(helm repo list -o json | jq -r '.[] | select(.url | match("'$REPO_URL'/*")) | .name')
    if [ -n "$REPO_NAME" ] ; then
      CHART_INFO=$(helm show chart "$REPO_NAME/$NAME")
    else
      echo "FATAL: Local repository declaration for $REPO_URL is missing"
      echo "Please add it using 'helm repo add <REPO_NAME> $REPO_URL' before retrying"
      exit 1
    fi
  fi
  APP_VERSION=$(yq -r .appVersion <<<"$CHART_INFO")
  LATEST_VERSION=$(yq -r .version <<<"$CHART_INFO")

  if [ "$VERSION" != "$LATEST_VERSION" ] ; then
    touch $CHART_NEEDS_VERSION_UP_FILE
    yq_update_value_inplace ".dependencies[$INDEX].version" "$LATEST_VERSION" Chart.yaml
    if [ "$NAME" == "crowdsec" ] ; then
      yq_update_value_inplace ".appVersion" "$APP_VERSION" Chart.yaml
    fi
  fi
  echo
done

echo "** Updating WebUI to latest"
WEBUI_LATEST_TAG=$(curl -fs "https://api.github.com/repos/theduffman85/crowdsec-web-ui/tags" | jq -r '.[] | .name' | sort -Vr | head -n1)
WEBUI_CURRENT_TAG=$(yq '.webui.image.tag' values.yaml)

if [ "$WEBUI_CURRENT_TAG" != "$WEBUI_LATEST_TAG" ] ; then
  yq_update_value_inplace .webui.image.tag "$WEBUI_LATEST_TAG" values.yaml
  touch $CHART_NEEDS_VERSION_UP_FILE
fi
echo

if test -e $CHART_NEEDS_VERSION_UP_FILE ; then
  echo "** Updating helm dependencies"
  helm dep up > /dev/null
  echo

  echo "==> Updating chart version as at least one component was updated"
  NEXT_CHART_VERSION=$(next_chart_version)
  yq_update_value_inplace ".version" "$NEXT_CHART_VERSION" Chart.yaml
else
  echo "Nothing changed."
fi

echo "All done."