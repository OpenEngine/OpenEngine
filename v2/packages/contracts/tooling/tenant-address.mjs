// Account identity is inserted by NATS, never by a public address template.
export function hasAccountToken(address) {
  return (address ?? "").split(".").includes("{account}");
}
