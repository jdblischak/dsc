# dscrutils

## Quick Start

1. Use devtools to install the most recent version of the R package
   from Github:

   ```R
   devtools::install_github("stephenslab/dsc",subdir = "dscrutils",
                            force = TRUE)
   ```

   See below for additional installation options.

2. Load the package, and run the `dscquery` example:

   ```R
   library(dscrutils)
   example("dscquery")
   ```

3. Explore the package documentation:

   ```R
   help(package = "dscrutils")
   ```

## Development notes

To install the package from a local copy of the git repository, run
the following command in R with the working directory set to the
repository root:

```R
getwd() # Should be .../dsc
devtools::install_local("dscrutils")
```

To test a local copy of the package, run the following command from
the repository root:

```R
getwd() # Should be .../dsc
devtools::test("dscrutils")
```

To build and test the package, run these commands from the
command-line shell:

```bash
R CMD build dscrutils
R CMD check --as-cran dscrutils_0.3.8.7.tar.gz
```

To update the package documentation from the
[roxygen2](http://r-pkgs.had.co.nz/man.html) tags, run the following
from R after first making sure your working directory is inside the
`dscrutils` directory:

```R
library(roxygen2)
roxygenize()
```
