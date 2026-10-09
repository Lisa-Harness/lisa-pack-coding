# Fixture repos hold deliberately broken code and acceptance/ holds hidden
# scenario judges for the e2e harness; neither is a pack test.
collect_ignore_glob = ["fixtures/*", "acceptance/*"]
