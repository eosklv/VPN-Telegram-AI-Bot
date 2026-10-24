variable "zone" {
  description = "Yandex Cloud availability zone"
  type        = string
  default     = "ru-central1-d"
}

variable "platform_id" {
  description = "Compute platform"
  type        = string
  default     = "standard-v3"
}

variable "cores" {
  description = "vCPU count"
  type        = number
  default     = 2
}

variable "memory" {
  description = "RAM in GB"
  type        = number
  default     = 2
}

variable "core_fraction" {
  description = "Guaranteed vCPU share in percent"
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = "Public key for the vpn-admin user; null disables the user"
  type        = string
  default     = null
}

variable "ssh_allowed_cidrs" {
  description = "CIDRs allowed to reach SSH; empty keeps port 22 closed"
  type        = list(string)
  default     = []
}

variable "protocol" {
  description = "VPN protocol: wireguard or openvpn"
  type        = string
  default     = "wireguard"
}

variable "wireguard_peers" {
  description = "WireGuard client names, one profile and QR code per name"
  type        = list(string)
  default     = ["phone", "laptop"]
}

variable "profile_upload" {
  description = "Presigned S3 POST (url and form fields) for uploading client profiles"
  type = object({
    url    = string
    fields = map(string)
  })
  default   = null
  sensitive = true
}
