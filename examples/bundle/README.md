# Sample bundle

A minimal forge bundle: a 9 km / 3 km two-domain nest over San Francisco
Bay on WRF's stock CONUS physics suite, forced by GFS. It exists so the
image can be exercised without an account anywhere:

    docker run --rm --user "$(id -u):$(id -g)" \
      -v $(pwd)/examples/bundle:/bundle:ro -v /path/to/WPS_GEOG:/geog:ro -v $(pwd)/out:/out \
      registry.frothy.surf/frothy-forge:latest --hours 6

CI runs it with --dry-run (stage everything, fetch and run nothing) as an
unprivileged user on every build. Real bundles come from forge.frothy.surf
and have the same four files.
