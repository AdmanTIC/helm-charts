#!/bin/bash

set -ex
rm -rf packaged-charts
mkdir packaged-charts
for CHART in charts/*/ ;
do
  helm package -d packaged-charts $CHART ;
done;

for CHART in packaged-charts/* ; do helm ppush --force $CHART chartmuseum-adm ; done
