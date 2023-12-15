# RBAC Namespace manager

## Description

This Chart will create a ServiceAccount and grant full-access in the namespace it has been deployed by deploying K8s RBAC. At the end of the deployment, it will give you a command to type to generate the corresponding `kubeconfig`.

## Usage

You can use the Chart as a sub-chart of your application or you can deploy it manually in the same namespace as your application :

```bash
$ helm upgrade -in $NAMESPACE --create-namespace rbac-ns-manager --repo=https://chartmuseum.admantic.fr/ rbac-ns-manager --set publicApiURL=$publicApiURL
```

`publicApiURL` is mandatory (URL syntax: `http[s]://<HOST>:<PORT>`). 

## Caution

> :warning: **Warning**: It is recommended to not use this Chart in system namespaces (e.g., `kube-system`) for security reasons. Please, only use it in application namespaces.