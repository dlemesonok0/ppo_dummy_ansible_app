#!/usr/bin/python
"""Minimal module for tracing the Ansiballz execution path."""

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r'''
---
module: echo_probe
short_description: Return a string without changing the target
description:
  - Demonstrate argument validation and JSON results through Ansiballz.
options:
  message:
    description: String returned to the controller.
    type: str
    required: true
author:
  - Dmitry Lemeshonok
'''

EXAMPLES = r'''
- name: Trace a custom module
  echo_probe:
    message: Hello Ansiballz
'''

RETURN = r'''
message:
  description: Validated input string.
  type: str
  returned: success
'''


def main():
    module = AnsibleModule(
        argument_spec=dict(message=dict(type="str", required=True)),
        supports_check_mode=True,
    )
    module.exit_json(changed=False, message=module.params["message"])


if __name__ == "__main__":
    main()
