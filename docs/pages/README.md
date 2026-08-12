# GitHub Pages site

This directory is a self-contained static site. The signup form sends this JSON to the URL in
the repository variable `SIGNUP_ENDPOINT`:

```json
{
  "email": "person@example.com",
  "source": "github-pages",
  "consentVersion": "2026-08-12"
}
```

The endpoint must accept `POST` and `OPTIONS` requests from `https://www.openengine.sh`, return a
2xx response for a successful or duplicate signup, and rate-limit abusive clients. Never put a
database password or service-role key in this directory or in `SIGNUP_ENDPOINT`.

## Deploy to `www.openengine.sh`

You need admin access to `spiralsoft-ai/openengine`, control of the `openengine.sh` DNS zone, and a
public HTTPS signup endpoint (the Supabase setup below is one option).

### 1. Verify the domain with GitHub

Do this before pointing DNS at Pages so another GitHub account cannot claim the domain:

1. Open the `spiralsoft-ai` organization **Settings → Pages**.
2. Select **Add a domain**, enter `openengine.sh`, and copy the TXT record GitHub provides.
3. Add that TXT record at the DNS provider and wait for it to resolve.
4. Return to the organization Pages settings and select **Verify**. Keep the TXT record in DNS.

Do not add a wildcard (`*`) DNS record for this domain.

### 2. Configure GitHub Pages

1. In `spiralsoft-ai/openengine`, open **Settings → Pages**.
2. Under **Build and deployment**, select **GitHub Actions** as the source.
3. Under **Custom domain**, enter `www.openengine.sh` and select **Save**.

This repository uses a custom Actions workflow, so a checked-in `CNAME` file is neither needed nor
used.

### 3. Configure DNS

Add this record at the DNS provider:

| Type | Name | Value |
| --- | --- | --- |
| `CNAME` | `www` | `spiralsoft-ai.github.io` |

Point the CNAME directly to the organization Pages hostname. Do not append `/openengine`.

To redirect the bare `openengine.sh` domain to `www.openengine.sh`, also add all four apex records:

| Type | Name | Value |
| --- | --- | --- |
| `A` | `@` | `185.199.108.153` |
| `A` | `@` | `185.199.109.153` |
| `A` | `@` | `185.199.110.153` |
| `A` | `@` | `185.199.111.153` |

Remove conflicting `A`, `AAAA`, or `CNAME` records for the same names. DNS changes can take up to
24 hours. Check them with:

```bash
dig +short www.openengine.sh CNAME
dig +short openengine.sh A
```

### 4. Configure and run the deployment

After deploying the signup endpoint below:

1. Open repository **Settings → Secrets and variables → Actions → Variables**.
2. Add `SIGNUP_ENDPOINT` with the public Edge Function URL. This is a variable, not a secret; it is
   intentionally sent to the browser. Never use a service-role key as its value.
3. Merge this workflow to `main`, or open **Actions → Deploy GitHub Pages → Run workflow**.
4. Wait for the `github-pages` environment deployment to succeed.
5. Return to **Settings → Pages** and enable **Enforce HTTPS** once GitHub provisions the
   certificate.

The canonical address is `https://www.openengine.sh/`. GitHub will redirect HTTP to HTTPS after
HTTPS enforcement is enabled and will redirect the apex domain when both DNS variants are set up.

## Recommended storage: Supabase Edge Function + Postgres

Do not write directly from the browser with a privileged database key. Put a small serverless
function in front of the table so secrets remain server-side and abuse controls have one home.

### 1. Create and link a Supabase project

Create a Supabase project, install its CLI, then authenticate and link this checkout:

```bash
supabase login
supabase link --project-ref PROJECT_REF
```

### 2. Create the table

Run this in the Supabase SQL editor:

```sql
create table public.interest_signups (
  email text primary key check (email = lower(trim(email))),
  created_at timestamptz not null default now(),
  source text not null,
  consent_version text not null,
  confirmed_at timestamptz
);

alter table public.interest_signups enable row level security;
```

Do not create a public table policy. The Edge Function uses its server-side secret to insert.

### 3. Create the function

Create `supabase/functions/interest-signup/index.ts`:

```ts
import { createClient } from "npm:@supabase/supabase-js@2";

const allowedOrigin = "https://www.openengine.sh";
const corsHeaders = {
  "Access-Control-Allow-Origin": allowedOrigin,
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);
  if (request.headers.get("origin") !== allowedOrigin) return json({ error: "Forbidden" }, 403);

  try {
    const body = await request.json();
    const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
    const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && email.length <= 254;
    if (!validEmail) return json({ error: "Valid email required" }, 422);

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );
    const { error } = await supabase.from("interest_signups").upsert(
      {
        email,
        source: body.source === "github-pages" ? body.source : "unknown",
        consent_version:
          typeof body.consentVersion === "string" ? body.consentVersion : "unknown",
      },
      { onConflict: "email", ignoreDuplicates: true },
    );

    if (error) throw error;
    return json({ subscribed: true });
  } catch (error) {
    console.error(error);
    return json({ error: "Could not save signup" }, 500);
  }
});
```

For local testing, make the allowed origin an environment variable rather than permitting every
origin in production.

### 4. Deploy and connect it

With the Supabase CLI authenticated and linked to the project:

```bash
supabase functions deploy interest-signup --no-verify-jwt
```

The form does not authenticate users, so the function must allow unauthenticated calls. Its URL is:

```text
https://PROJECT_REF.supabase.co/functions/v1/interest-signup
```

Put that URL in the GitHub repository variable `SIGNUP_ENDPOINT`, then re-run the Pages workflow.
Test a real submission and verify the row appears in `public.interest_signups`.

To inspect or export signups, use the Supabase table editor or run a read-only query in its SQL
editor:

```sql
select email, created_at, source, consent_version, confirmed_at
from public.interest_signups
order by created_at desc;
```

### 5. Production checklist

Before promoting the page widely, add IP-based rate limiting or a CAPTCHA such as Cloudflare
Turnstile in the Edge Function. Add double opt-in through the provider that sends your email, and
record `confirmed_at` only after the subscriber confirms. Publish a privacy notice explaining what
you collect, why, how long you retain it, and how someone can unsubscribe or request deletion.

Verify the complete path before announcing the page:

- `https://www.openengine.sh/` loads without mixed-content or certificate errors.
- A valid address produces the success state and one database row.
- Submitting the same normalized address again succeeds without creating another row.
- An invalid address is rejected, and a simulated backend failure shows the retry message.
- The service-role key exists only in Supabase-managed secrets, not in GitHub variables or site
  files.

If Pages deploys but the form says signup is not configured, confirm `SIGNUP_ENDPOINT` is defined
as a repository Actions variable and re-run the workflow. If the browser reports a CORS error,
confirm the function's `allowedOrigin` is exactly `https://www.openengine.sh` and redeploy it.
