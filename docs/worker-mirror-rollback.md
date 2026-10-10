# Signed Worker mirror rollback

Rollback remains signed, generated, and client-managed. Direct edits to a
public mirror or personal Worker are never a recovery mechanism.

## Preconditions

- No active managed run, Artifact import, Mailbox cleanup, or token lease.
- Current public and personal Worker commit/tree and manifest digest recorded.
- The target snapshot was previously approved and is not revoked.
- Root trust metadata still authorizes its release key and protocol range.

## Procedure

1. Export the target private source commit through the protected release
   workflow to a new generated branch.
2. Review the public PR and require mirror-policy, unit, protocol, boundary,
   history, and secret-scanning checks.
3. Merge without force-pushing or rewriting public history.
4. Update the private client pin with the new public commit, tree, manifest
   digest, signing key ID, trust epoch, and protocol range.
5. Repair the personal Worker through the client and verify exact tree equality.
6. Run an encrypted echo and require verified Artifact import plus zero
   Mailbox temporary content, leases, temporary token, and pending cleanup.

A workflow dispatch, repair request, or accepted operation is not success. Only
backend-confirmed signature, tree, protocol, and cleanup evidence completes a
rollback. There is no `legacy-template` mode or local-compute fallback.
