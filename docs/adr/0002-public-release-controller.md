# ADR 0002: Public protected release controller

- Status: accepted
- Date: 2026-07-21

## Context

The private repository owner plan does not support required reviewers for a
private Environment. GitHub creates the Environment but rejects the reviewer
protection rule. Storing the publisher or release signing private key in an
unprotected private repository secret would violate ADR 0001.

The public repository does support required reviewers and protected-branch
deployment policies.

## Decision

The trusted release controller runs only from protected public `main` and uses
the public repository's `worker-mirror-release` Environment.

- A source-reader GitHub App is installed only on the private monorepo and has
  metadata read plus contents read.
- A publisher GitHub App is installed only on the public mirror and has metadata
  read, contents write, workflows write and pull-requests write. GitHub requires
  the workflows permission because the generated snapshot owns files below
  `.github/workflows/`; it grants no Actions secrets or administration access.
- Both App private keys and the release signing key exist only in the protected
  public Environment.
- The workflow requires an exact private source commit, checks that it is an
  ancestor of private `main`, exports and signs in a clean checkout, then pushes
  only an orphan-derived snapshot branch to the public repository.
- Pull-request workflows receive none of these secrets.

The initial controller workflow is introduced by a one-time bootstrap PR. Once
the first generated mirror merges, the controller itself is generated from the
private monorepo like every other public file.

## Consequences

This is not a bypass of GitHub review protection. It relocates the release
boundary to a repository where GitHub can enforce the required reviewer. The
private repository remains the only source of truth and gains no long-lived
cross-repository credential.
