variable "subscription_id" { type = string }
variable "location" { type = string }
variable "prefix" { type = string }
variable "subnet_id" { type = string }
variable "admin_cidr" { type = string }
variable "hostname" { type = string }
variable "vm_size" { type = string }
variable "image_urn" { type = string }
variable "actions_enabled" { type = bool }
variable "ssh_public_key" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}
