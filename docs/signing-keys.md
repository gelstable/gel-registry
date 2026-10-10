# Signing keys, custody and recovery

## Signing keys and Environment secrets

The production certificate uses an RSA 4096 certify-only primary key and a
signing subkey for automation. RSA provides compatibility with supported APT
and EL9 RPM clients. The certificate and metadata have no expiry; unattended
clients do not need periodic trust refreshes, but compromise requires explicit
rotation and an advisory.

| Repository | GitHub Environment | Secrets |
| --- | --- | --- |
| `gel-registry` | `registry-signing` | `REGISTRY_SIGNING_KEY`, `REGISTRY_SIGNING_FPR` |
| `gel`, `gel-cli`, `gel-postgis` | `package-signing` | `PACKAGE_SIGNING_KEY`, `PACKAGE_SIGNING_FPR` |

Restrict `registry-signing` to `main`; `package-signing` allows each product's
protected default branch and protected `release/*` branches. Key secrets
contain the exported signing subkey material; fingerprint secrets identify the
full **signing subkey** fingerprint, distinct from the primary fingerprint
published to users. Registry signing exposes `REGISTRY_SIGNING_FPR` to the CLI
as `GELSTABLE_SIGNING_FPR` in its temporary signing home. Keep the primary private key out of CI, Git and committed files.
Export only secret subkeys for the protected GitHub Environments, never the
primary private key. The current batch import and signing workflows have no
passphrase input: the automation subkey export must have no passphrase. Keep
that export protected by 1Password and GitHub Environment secret storage;
an encrypted export that needs an interactive unlock will not work in CI.
An ordinary export of a passphrase-protected subkey retains that protection.
If removing a subkey passphrase for automation, work only on an isolated copy
containing secret subkeys and no primary private key; never remove protection
from the custody primary or its backup.
The primary private-key backup remains protected by its own strong passphrase.

## Local signing-key helper

`scripts/signing-key.py` wraps standard GnuPG locally using Python's standard
library. Install Python 3.13 and GnuPG 2.4 with a working pinentry; on macOS,
`brew install python@3.13 gnupg pinentry-mac` supplies them. Run from a private
operator terminal. For terminal pinentry, set `GPG_TTY="$(tty)"`; a graphical
pinentry can be selected with `--pinentry-program /absolute/path/to/pinentry-mac`.
Trusted executable paths resolve to their canonical executable, including
Homebrew symlinks; secret/input/output paths still refuse symlinks.
The helper creates isolated temporary homes and stops their agents, preserving
your existing `GNUPGHOME` and keyrings. It never installs secrets, publishes
keys, changes your public trust, or revokes custody/caller keys.

Choose a **new** output directory outside every Git checkout. Existing paths
and symlink destinations are refused. The directory is private (`0700`) and
all artifacts are private (`0600`), including the public certificate:

```sh
python3 scripts/signing-key.py create \
  --uid 'Gelstable Package Signing <maintainer-controlled-address>' \
  --out "$HOME/SigningCustody/gelstable-initial"
```

GPG first asks for a strong passphrase protecting the RSA4096 certify-only
primary and its RSA4096 signing subkey; neither expires under current policy.
Save this passphrase separately in custody. Generation, backup export and
subkey export may ask you to unlock that key again. In a **separate subkey-only
home**, GPG then asks for the existing passphrase followed by a new passphrase:
leave the new passphrase empty and confirm removal. The helper uses GPG agent's
[interactive PASSWD command](https://www.gnupg.org/documentation/manuals/gnupg/Agent-PASSWD.html)
for this selected subkey. The protected custody primary never undergoes that
change. The script never reads a passphrase and accepts no passphrase argument
or environment variable. Do not record the ceremony terminal or pinentry.

| Artifact | Custody purpose |
| --- | --- |
| `primary-backup.asc` | Passphrase-protected primary plus existing subkeys; offline recovery and future rotation |
| `automation-subkey.asc` | Selected **unprotected** signing secret only; source for Environment key secrets |
| `public.asc` | Full public certificate, retaining historical public subkeys |
| `fingerprints.json` | Primary, selected signing and historical subkey fingerprints; artifact integrity hashes |
| `primary-revocation.rev` | Primary emergency revocation artifact, with GPG's import-protection colon intact |
| `README.txt` | Bundle custody and manual recovery reminders |

Upload these files as separate 1Password Documents or attachments and store the
primary passphrase in the separate custody item described below. Protect the
unprotected automation export with vault and Environment access controls. Never
upload the primary backup or its passphrase as a CI signing secret. Compare the
printed fingerprints with the independently recorded ceremony inventory before
using any exported key.

For routine signing-subkey replacement, download the protected primary backup
**and its original primary revocation artifact** to private files. Supply the
recorded full primary fingerprint, rather than a UID or short key ID:

```sh
python3 scripts/signing-key.py rotate-subkey \
  --backup /secure/downloads/primary-backup.asc \
  --revocation-certificate /secure/downloads/primary-revocation.rev \
  --primary-fingerprint '<40-character recorded primary fingerprint>' \
  --out "$HOME/SigningCustody/gelstable-rotation-2026-10"
```

The prompt sequence is the same: unlock the protected primary to certify the
replacement, then remove protection only from the new isolated automation copy.
The refreshed primary backup and public certificate retain historical subkeys.
The supplied revocation artifact is authenticated against the exact primary
before replacement key generation, then carried forward unchanged. No custody
or caller key is revoked or deleted.
If the primary is compromised, use `create` and the new-trust procedure below.

The helper verifies every completed bundle. Also verify downloaded vault copies
in a new private directory, using fingerprints from your independent inventory:

```sh
python3 scripts/signing-key.py verify \
  --bundle /secure/downloads/gelstable-bundle \
  --primary-fingerprint '<40-character recorded primary fingerprint>' \
  --signing-fingerprint '<40-character recorded signing-subkey fingerprint>'
```

`verify` checks artifact integrity, matching certificate fingerprints and key
policy, protected primary backup, absence of primary or unrelated CI secrets,
and a test signature from the exact signing subkey. Fresh isolated agents with
`--pinentry-mode error` ensure an encrypted/nonfunctional CI export fails rather
than succeeding through a cached passphrase. Public-only verification checks the
actual signing fingerprint. All four artifact hashes must be present and valid;
they provide consistency, not a replacement for independently recorded
fingerprints. Revocation authentication uses a separate disposable public-only
copy: that copy must begin unrevoked and become revoked by the supplied
certificate for exactly the recorded primary. The import-protection colon is
removed only from the temporary validation copy; the original artifact, custody
primary and caller keyrings stay unchanged. Separately perform
the manual primary-unlock restore test below with the saved passphrase.

On cancellation or failure, temporary homes are cleaned up and output retains
an `INCOMPLETE` marker and any backups already exported. Keep those backups,
investigate the failure and choose a new output directory for another ceremony.
Do not install an incomplete bundle. After verified vault restore and manual
primary unlock, remove local exports according to custody policy.

Continue the reviewed key-rotation procedure below: update GitHub Environment
secrets, public certificates and fingerprints, the Docker image's primary-key
fingerprint pin, and re-sign all bootstrap metadata.
These remain operator actions. Emergency revocation activation and distribution
also remain manual; never remove the `.rev` colon or import it during routine
restore or rotation.

## Signing-key custody and recovery

While the project has one maintainer, a personal 1Password account with a
dedicated **Gelstable Signing** vault is an acceptable custody model. Generate
the keys locally on a maintained Mac with disk encryption, in a protected
`GNUPGHOME` outside the checkout. A separate offline key-generation ceremony
and independent encrypted backups are optional stronger arrangements; a second
maintainer is not a prerequisite for the solo setup.

Keep these project items in the dedicated vault:

| Item | Contents |
| --- | --- |
| Primary private-key backup | Passphrase-protected private-key export for certification and recovery; never upload it to CI. |
| Primary-key passphrase | The strong passphrase for the primary backup, recorded separately from the backup item. |
| Signing-subkey backup | Secret-subkey-only export matching the automation passphrase policy above. |
| Revocation certificate | The certificate needed to revoke the primary if it is compromised; treat it as sensitive. |
| Public certificate and fingerprints | Public certificate plus full primary and signing-subkey fingerprints, clearly labeled. |
| Recovery and rotation guide | This procedure, item/file inventory, restore-test record and custodian details; no secrets in the repository copy. |

Store key exports and the revocation certificate as Document items or attached
files, following [1Password's file instructions](https://support.1password.com/files/).
Before deleting temporary exports, download the saved backups and restore them
into isolated temporary `GNUPGHOME` directories with mode `700` outside the
checkout. Compare the primary and subkey fingerprints with the recorded public
certificate using `gpg --with-fingerprint --with-subkey-fingerprint` when
listing restored keys. Confirm the primary backup unlocks with its saved
passphrase. In a separate home containing only the automation export, confirm
the primary secret is absent and make a batch test signature selecting the
full signing-subkey fingerprint with `!`. Use a fresh agent with no cached
passphrase and `--pinentry-mode error` to prove no interactive unlock is needed;
verify that signature using only the public certificate and check the
signing-subkey fingerprint. Record the result,
stop the temporary GPG agents, and remove the temporary homes and exports.

Prepare the account [Emergency Kit](https://support.1password.com/emergency-kit/)
and an [individual-account recovery code](https://support.1password.com/recovery-codes/).
Keep accessible protected copies outside this same vault, such as in a physical
safe, so losing vault access does not also lose recovery access. Plan recovery
of the account's email and authenticator/2FA separately: recovery-code use needs
email access and leaves enabled 2FA in place. Update the Emergency Kit after
account credentials change. These account-recovery materials are personal
account credentials, separate from the project's key-custody items.

When other maintainers join, move only project items into a restricted
**Gelstable Signing** custody vault in an organization/team 1Password account.
Use the [account migration instructions](https://support.1password.com/migrate-business-account/)
for moving items, choosing the shared custody vault rather than an Employee or
private vault. Verify every item and all Document/attachment files are present
and downloadable in the destination, then repeat the restore test before
removing old copies. Assign appropriate organization owners and designated
key custodians, with actual item access and a working account-recovery path;
review [vault permissions](https://support.1password.com/create-share-vaults-teams/),
including inherited group and owner access. Contributors who only review or
approve releases do not need key custody. Team account recovery uses
administrators rather than individual recovery codes.

Moving custody preserves the key fingerprints and does not require rotation
unless compromise is suspected. Remove obsolete copies and access after the
verified handoff, but neither deletion nor removing vault access cryptographically
revokes a key already copied by someone. Use the [compromise checklist](operations.md#key-compromise)
when previously exposed key material can no longer be trusted.


## Rotation and revocation

Routine rotation adds a signing subkey using the helper above and preserves old
subkeys so published RPMs stay trusted. Set the eight Environment secrets with
`scripts/set-signing-secrets.sh BUNDLE_DIR`, then replace the committed public
certificate by PR. Follow [the rotation checklist](operations.md#routine-key-rotation).

For a compromised subkey, work in the restored encrypted custody home:

```sh
gpg --homedir /ENCRYPTED/gelstable-key --edit-key PRIMARY_FINGERPRINT
# At the prompt: list; key N (select the compromised subkey); revkey; save.
gpg --homedir /ENCRYPTED/gelstable-key --armor --export PRIMARY_FINGERPRINT > revoked-public.asc
```

Make a scratch copy and check that the selected subkey, rather than the primary,
is revoked (`gpg --homedir /ENCRYPTED/gelstable-key --with-colons --list-keys
PRIMARY_FINGERPRINT` must show `sub:r` for its fingerprint and an unrevoked
primary). Export a refreshed passphrase-protected secret backup from that home;
the original backup predates the revocation:

```sh
gpg --homedir /ENCRYPTED/gelstable-key --armor --export-secret-keys PRIMARY_FINGERPRINT > /ENCRYPTED/revoked-primary-backup.asc
python3 scripts/signing-key.py rotate-subkey \
  --backup /ENCRYPTED/revoked-primary-backup.asc \
  --revocation-certificate /ENCRYPTED/primary-revocation.rev \
  --primary-fingerprint PRIMARY_FINGERPRINT \
  --out /ENCRYPTED/replacement-bundle
gpg --with-colons --import-options show-only --import /ENCRYPTED/replacement-bundle/public.asc
```

Verify the replacement public certificate still lists the compromised subkey
as `sub:r` and includes the new signing subkey. Keep the refreshed secret backup
and revocation evidence encrypted. Follow [the compromise checklist](operations.md#key-compromise)
for the certificate/yank PR, automated metadata signing, repacks and advisory.
No operator signs repository metadata locally.
