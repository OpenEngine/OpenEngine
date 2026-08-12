const form = document.querySelector("#interest-signup");
const emailInput = document.querySelector("#signup-email");
const submitButton = form.querySelector("button[type='submit']");
const status = document.querySelector("#signup-status");

function showStatus(message, kind = "") {
  status.textContent = message;
  status.className = `form-status ${kind}`.trim();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showStatus("");

  if (!form.checkValidity()) {
    form.reportValidity();
    return;
  }

  // Quietly accept obvious bot submissions. The endpoint still needs real rate limiting.
  if (form.elements.website.value) {
    form.reset();
    showStatus("You’re on the list. We’ll be in touch.", "success");
    return;
  }

  const endpoint = window.OPENENGINE_CONFIG?.signupEndpoint?.trim();
  if (!endpoint) {
    showStatus("Signup is not configured yet. Please check back soon.", "error");
    return;
  }

  emailInput.disabled = true;
  submitButton.disabled = true;
  submitButton.textContent = "Joining…";

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: emailInput.value.trim().toLowerCase(),
        source: "github-pages",
        consentVersion: "2026-08-12",
      }),
    });

    if (!response.ok) {
      throw new Error(`Signup endpoint returned ${response.status}`);
    }

    form.reset();
    showStatus("You’re on the list. We’ll be in touch.", "success");
  } catch (error) {
    console.error(error);
    showStatus("We couldn’t add you right now. Please try again.", "error");
  } finally {
    emailInput.disabled = false;
    submitButton.disabled = false;
    submitButton.textContent = "Keep me posted";
  }
});
