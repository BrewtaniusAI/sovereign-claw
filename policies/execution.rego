package sovereign_claw

default execution := {
  "allow": true,
  "deny": [],
  "matched": []
}

execution := result if {
  deny_reasons := [reason |
    input.tool == "shell_exec"
    reason := "shell_exec is blocked by bundled rego policy"
  ]
  matched := [policy |
    input.tool == "shell_exec"
    policy := "rego.block_shell_exec"
  ]
  count(deny_reasons) > 0
  result := {
    "allow": false,
    "deny": deny_reasons,
    "matched": matched,
  }
}
