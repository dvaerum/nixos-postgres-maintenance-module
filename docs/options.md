## _module\.args

Additional arguments passed to each module in addition to ones
like ` lib `, ` config `,
and ` pkgs `, ` modulesPath `\.

This option is also available to all submodules\. Submodules do not
inherit args from their parent module, nor do they provide args to
their parent module or sibling submodules\. The sole exception to
this is the argument ` name ` which is provided by
parent modules to a submodule and contains the attribute name
the submodule is bound to, or a unique generated name if it is
not bound to an attribute\.

Some arguments are already passed by default, of which the
following *cannot* be changed with this option:

 - ` lib `: The nixpkgs library\.

 - ` config `: The results of all options after merging the values from all modules together\.

 - ` options `: The options declared in all modules\.

 - ` specialArgs `: The ` specialArgs ` argument passed to ` evalModules `\.

 - All attributes of ` specialArgs `
   
   Whereas option values can generally depend on other option values
   thanks to laziness, this does not apply to ` imports `, which
   must be computed statically before anything else\.
   
   For this reason, callers of the module system can provide ` specialArgs `
   which are available during import resolution\.
   
   For NixOS, ` specialArgs ` includes
   ` modulesPath `, which allows you to import
   extra modules from the nixpkgs package tree without having to
   somehow make the module aware of the location of the
   ` nixpkgs ` or NixOS directories\.
   
   ```
   { modulesPath, ... }: {
     imports = [
       (modulesPath + "/profiles/minimal.nix")
     ];
   }
   ```

For NixOS, the default value for this option includes at least this argument:

 - ` pkgs `: The nixpkgs package set according to
   the ` nixpkgs.pkgs ` option\.



*Type:*
lazy attribute set of raw value



*Default:*

```nix
{ }
```

*Declared by:*
 - [\<nixpkgs/lib/modules\.nix>](https://github.com/NixOS/nixpkgs/blob//lib/modules.nix)



## services\.postgresqlCollationGuard\.enable



Whether to enable the Postgres collation-drift guard\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.package



The ` collation-guard ` package to run\. Wired automatically to this
flake’s own ` packages.<system>.default ` by ` nixosModules.default ` –
override only to test a different build\.



*Type:*
package

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure



Executables run, in order, after the guard fails (e\.g\. to alert
on-call or trigger a restore)\. Runs even if the guard’s own
process is killed or crashes, via the unit’s ` OnFailure= `
dependency – not just on a clean non-zero exit\.
` COLLATION_GUARD_CONTEXT ` carries
` {"stage": "on_failure", "failures": [{"database": ..., "relation": ..., "error": ...}, ...]} `\.



*Type:*
list of package



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess



Executables run, in order, after the guard completes with no
failures (e\.g\. to notify success or prune old pre-start
backups)\. ` COLLATION_GUARD_CONTEXT ` carries
` {"stage": "on_success", "databases_processed": [...], "databases_repaired": [...]} `\.



*Type:*
list of package



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun



Executables run, in order, after the guard finishes – always,
whether it succeeded or failed (e\.g\. to emit a single
run-completed metric/notification regardless of outcome)\.
` COLLATION_GUARD_CONTEXT ` carries
` {"stage": "post_run", "success": true|false} `\.



*Type:*
list of package



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart



Executables run, in order, before the guard examines any
database (e\.g\. to take a pre-emptive backup)\. Each must exit 0;
a non-zero exit aborts the guard before any check or repair
runs\. Invoked with no arguments; JSON context is passed via the
` COLLATION_GUARD_CONTEXT ` environment variable
(` {"stage": "pre_start"} `)\.



*Type:*
list of package



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.partitionRepair\.enable



Also check text-keyed, non-` C `/` POSIX `-collated partition bounds
and repair any row that’s drifted into the wrong physical
partition under the new collation\. See
docs/decisions/0004-cross-partition-update-for-partition-repair\.md\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.partitionRepair\.maxRepairAttempts



Safety cap on the per-row repair retry loop per partitioned
table, so a pathological number of misplaced rows fails loudly
instead of spinning forever\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
1000
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)


