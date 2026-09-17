# Registering Bindery with D3 Auth

`bindery.d3auth.json` is the manifest. Register it in the D3 Auth console (Apps → Register,
paste the file), then copy the values from the **connection sheet** the console shows — never
from this file or from any documentation, because the sheet is generated from what the provider
actually does.

Then, in Bindery's Settings:

| Field | From the sheet |
|---|---|
| Issuer | the issuer URL, e.g. `https://auth.d3cloud.io` |
| Client id | `bindery` |
| Client secret | shown **once**, at registration or after a rotation |
| Mode | `optional` to add it beside the password form, `required` to lead with it |

`optional` changes nothing for accounts that never use it. `required` keeps the owner's local
sign-in reachable at `/login/local`, which is the way back in when the provider is down.

The redirect URI is matched character for character at the provider. An instance on another
hostname needs its own manifest with its own URIs, and `OIDC_REDIRECT_URI` set to match.
