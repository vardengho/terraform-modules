variable "family" {
    type = string
}

variable "environment" {
    type = string
    default = "Development"
}

variable "tags" {
    type = map(string)
    default = {}
}

variable "dsn" {
    type = string
    sensitive = true
}

variable "additional_values" {
    type = map(string)
    default = {}
}

variable "log_groups" {
    type = list(object({
        arn = string
        name = string
    }))
    default = []
}

variable "timeout" {
    type = number
    default = 60
}

variable "memory_size" {
    type = number
    default = 512
}

variable "filter_pattern" {
    type = string
    default = null
}
