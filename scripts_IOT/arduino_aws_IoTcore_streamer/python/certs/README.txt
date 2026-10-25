Put the AWS IoT Core files for this board here (from Karthika):

  AmazonRootCA1.pem     Amazon root CA 1 (https://www.amazontrust.com/repository/AmazonRootCA1.pem)
  device.pem.crt        this board's device certificate
  private.pem.key       this board's private key  (keep secret - never commit or share)

File names can be changed in ../aws_config.json ("ca_cert", "cert", "key").
The app checks every 30 s and connects as soon as all three exist and the
endpoint in aws_config.json is set.
