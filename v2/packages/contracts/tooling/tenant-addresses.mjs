// Only NATS may insert account identity when forwarding to shared services.
export function accountTokenChannels(service, document) {
  if (service.startsWith("internal/")) return [];
  return [...document.channels()].filter((channel) =>
    (channel.address() ?? "").split(".").includes("{account}")
  );
}
