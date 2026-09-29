package ratifyverification

import future.keywords.if

# Gatekeeper provides external_data; this stub lets plain OPA compile and mock it.
external_data(req) := {"system_error": "", "errors": [], "responses": []}

pod := {"review": {"object": {"spec": {"containers": [{"image": "ghcr.io/o/r@sha256:aa"}], "initContainers": [{"image": "ghcr.io/o/init@sha256:bb"}]}}}}

ok(req) := {"system_error": "", "errors": [], "responses": [[k, {"isSuccess": true}] | k := req.keys[_]]}
unsigned(req) := {"system_error": "", "errors": [], "responses": [[k, {"isSuccess": k != "ghcr.io/o/init@sha256:bb"}] | k := req.keys[_]]}
down(req) := {"system_error": "provider timeout", "errors": [], "responses": []}
errs(req) := {"system_error": "", "errors": [["ghcr.io/o/r@sha256:aa", "no signature"]], "responses": []}
nosuccess(req) := {"system_error": "", "errors": [], "responses": [[k, {}] | k := req.keys[_]]}

test_signed_pod_allowed if { count(violation) == 0 with input as pod with external_data as ok }
test_unsigned_init_container_denied if { count(violation) == 1 with input as pod with external_data as unsigned }
test_provider_outage_fails_closed if { count(violation) > 0 with input as pod with external_data as down }
test_per_image_error_denied if { count(violation) > 0 with input as pod with external_data as errs }
test_missing_success_field_denied if { count(violation) > 0 with input as pod with external_data as nosuccess }
