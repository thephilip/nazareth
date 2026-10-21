`docker-agent-schema.json` is `agent-schema.json` from
[docker/docker-agent](https://github.com/docker/docker-agent) at tag `v1.152.0`, unmodified.
`tests/test_docker_agent.py` validates the generated team config against it, so the tests
need neither the docker-agent binary nor network access. Refresh it deliberately when
targeting a newer Docker Agent.
