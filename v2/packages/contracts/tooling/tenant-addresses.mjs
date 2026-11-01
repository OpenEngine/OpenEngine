// The workspace runtime is a separate internal contract, not a tenant API.
export function tenantAddressProblems(service, channels) {
  if (service.startsWith("internal/")) return [];
  return [...channels]
    .filter((channel) => (channel.address() ?? "").split(".").includes("{account}"))
    .map((channel) => `${service}: error account-token: channel ${channel.id()} address ${channel.address()} contains {account}`);
}
