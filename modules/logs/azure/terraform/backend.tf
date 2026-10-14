# Partial backend config: mcctl passes -backend-config=path=<MCCTL_HOME>/state/<cloud>/<account>/<signal>.tfstate
terraform {
  backend "local" {}
}
