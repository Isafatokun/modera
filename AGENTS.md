# Engineering review

Before pushing code, perform and record separate review passes:

1. Correctness and contract behavior: trace the successful path, malformed inputs,
   partial failures, cancellation, deadlines, retries, and resource ownership.
2. Language and API design: use idiomatic constructs, keep public APIs small,
   document exported behavior and defaults, and avoid unnecessary abstractions.
3. Adversarial review and verification: challenge assumptions, check concurrency,
   credentials and bounds, add regression tests for concrete defects, then inspect
   the final diff after fixes. Run appropriate format, static, race, integration,
   and build checks. State any checks that could not run.

Do not call code production-ready or world-class merely because tests pass.
Distinguish software correctness from moderation accuracy and deployment evidence.
Wait for required CI checks on the exact revision before merging.

When publishing through file APIs, verify the created remote Git tree SHA matches
exactly the reviewed local tree before updating the branch. Reject truncated
transfer output; local test results apply only to identical published contents.
