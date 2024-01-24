# Public AdmanTIC Helm Charts

Library which gathers different custom Helm Charts.

## Prerequisites

* Kubernetes 1.23+
* Helm 3

## Usage

You can add the public Chartmuseum and install one of these charts:

```bash
$ helm repo add public-admantic-charts https://chartmuseum.admantic.fr
$ helm upgrade -in $NAMESPACE --create-namespace $NAME public-admantic-charts/$CHART
```

Or you can directly install the chart:

```bash
helm upgrade -in $NAMESPACE --create-namespace $NAME --repo=https://chartmuseum.admantic.fr/ public-admantic-charts/$CHART
```