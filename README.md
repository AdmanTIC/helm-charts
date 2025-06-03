# Public AdmanTIC Helm Charts

Library which gathers different custom Helm Charts.

## Prerequisites

* Kubernetes 1.23+
* Helm 3

## Usage

You can directly install the chart:

```bash
helm upgrade -in $NAMESPACE --create-namespace $NAME --repo=oci://ghcr.io/admantic/helm-charts $CHART
```