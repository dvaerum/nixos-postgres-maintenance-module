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



## services\.postgresqlCollationGuard\.connectionLockdown\.enable



Reject new connections to a database for exactly the
duration it’s actively being reindexed/repaired, and
terminate any session already connected to it at that
moment – closing the gap where a client not itself ordered
after ` postgresql-setup.service `/` postgresql.target ` (local
or remote, since ` postgresql.service ` is already accepting
connections by the time this guard runs) could otherwise
connect against inconsistent state\. A database with no stale
collation and no partition-repair-eligible table is never
locked – eligibility is a structural applicability check,
not proof a row is actually misplaced, so an eligible
database can still be locked and found clean\. Default ` true `
since this closes a real correctness gap, but every existing
deployment gets this behavior on the next upgrade with no
config change – turn it off here if there’s a specific
reason to allow concurrent connections during the guard’s
run\. See
docs/decisions/0007-connection-lockdown-during-repair\.md\.

The underlying mechanism is chosen automatically from the
configured ` services.postgresql.package ` version, with no
option change required: PostgreSQL 16+ uses a ` pg_hba.conf `
rule (the above); PostgreSQL \< 16 – which predates the
` pg_hba.conf ` directive that mechanism depends on – falls
back to ` ALTER DATABASE ... CONNECTION LIMIT `, which does
*not* reject a superuser connection (a narrower, but real and
clearly surfaced, guarantee)\. See
docs/decisions/0009-connection-limit-fallback-for-pg-lt-16\.md\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure



Run after the guard fails or crashes outright – even if the
guard’s own process is killed, via the unit’s ` OnFailure= `
dependency, not just a clean non-zero exit (the one guarantee
a dead process can’t arrange for itself)\. The *trigger* stays
systemd-native; the hooks themselves use the exact same
mechanism as every other stage\. ` blockOnFailure = true ` + a
non-zero exit makes the companion unit itself report failed
status (visible to ` systemctl --failed ` and anything
monitoring systemd unit health) – there’s nothing left to
block booting at this point, the main run has already failed\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess



Run, in order, once – only when every database processed with
zero failures (unlike ` postRun `, which always runs regardless
of outcome)\. ` blockOnFailure = true ` + a non-zero exit adds a
failure to an otherwise-clean run, flipping its exit code to 1\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure



Run after a database’s own processing fails\.
` COLLATION_GUARD_ERROR ` carries a summary of what failed\.
` blockOnFailure = true ` + a non-zero exit adds a second,
distinct failure entry alongside the database’s original one
– both visible independently\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess



Run after a database’s own processing succeeds\.
` blockOnFailure = true ` + a non-zero exit adds a failure for
that database even though its actual Postgres processing was
clean – for a notification that’s itself load-bearing\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart



Run, in order, before the guard examines *each* database
(e\.g\. a per-database backup) – ` COLLATION_GUARD_DATABASE `
names which one\. ` blockOnFailure = true ` + a non-zero exit
skips processing of that one database entirely (no reindex,
no partition repair, not counted as processed); every other
database in the same run is unaffected\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun



Run, in order, after the guard finishes – always, whether it
succeeded or failed, and after ` onSuccess ` if that also ran\.
` blockOnFailure = true ` + a non-zero exit adds a failure even
after every database already finished cleanly, which can flip
an otherwise-clean run’s exit code to 1\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart



Run, in order, before the guard examines any database (e\.g\. to
take a pre-emptive backup)\. ` blockOnFailure = true ` + a
non-zero exit aborts the whole run immediately, before
enumerating or connecting to any database\.



*Type:*
list of (submodule)



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.args



Extra arguments passed to the hook executable\.



*Type:*
list of string



*Default:*

```nix
[ ]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.blockOnFailure



Whether a non-zero exit from this hook should be treated as a
failure of whatever it’s attached to – aborting the whole run
immediately for ` preStart `; skipping just that one database
for ` perDatabase.preStart `; adding an extra failure entry
(which can flip an otherwise-successful run’s exit code) for
every other stage, including the ` onFailure ` companion unit’s
own reported status\. No default – must be set explicitly, or
evaluation throws naming the option path\. See the hook-point
descriptions below for exactly what each stage blocks\.



*Type:*
null or boolean



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.environment



Inline environment variables for this hook\. Merged with
` environmentFile ` and the stage’s own default variables
(` COLLATION_GUARD_STAGE `/` DATABASE `/` CONTEXT `/` ERROR `) – a key
defined by more than one of those three sources is a hard
error at run time (` EnvironmentCollisionError `), never a
silent override\. ` COLLATION_GUARD_CONTEXT ` is always present
and always valid JSON, on every stage (an empty ` {} ` where
there’s nothing yet to report) – no need to check whether it
exists before parsing it\. See docs/decisions/0006\.

**Never put a secret (token, password, API key) in here\.**
Every value is serialized by this module into a JSON file
built into the Nix store – world-readable, and pushed to any
configured binary cache – regardless of ` connectionLockdown `
or anything else\. Use ` environmentFile ` below for anything
sensitive\.



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file, merged with
` environment ` and the stage’s own default variables under the
same no-collision rule\. This is where a secret belongs: only
the *path* is written into the Nix store, never the file’s
contents, so point it at a decrypted sops-nix/agenix secret
(e\.g\. ` config.sops.secrets."my-hook-token".path `) or
equivalent – never at a plain file checked into this
repository or a Nix store path itself (which would defeat the
whole point, since store paths are world-readable)\.



*Type:*
null or absolute path



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.path



Executable to run, e\.g\. ` lib.getExe pkgs.curl ` or an explicit
path into a derivation’s own ` /bin ` directory\.



*Type:*
absolute path

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.timeoutSec



Per-hook timeout, applied to every one of the seven hook
points uniformly\. A hook has no inherent bound on how long
it can run, and every hook-running call in this project
blocks synchronously on it – a hung hook (a stuck webhook
script, a misconfigured notifier) would otherwise stall this
oneshot unit, and hence ` postgresql.target `, indefinitely\.
Defaults to systemd’s own ` DefaultTimeoutStartSec ` (90s) –
the bound a hung hook was already implicitly subject to via
the unit’s own start timeout, now enforced per-hook instead
and explicit rather than relying on that systemd default\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
90
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.maxParallelDatabases



How many databases to process concurrently (bounded, not
unbounded – all databases share the same Postgres instance’s
disk I/O, shared buffers, and WAL writer, so an unbounded
parallelism could make things slower, not faster, on a cluster
with many databases)\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
4
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.onFailureService\.group



OS group the ` postgresql-collation-guard-on-failure ` companion
unit runs as\. See ` user ` above for why it defaults to match
the main unit\.



*Type:*
string



*Default:*

```nix
"postgres"
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.onFailureService\.user



OS user the ` postgresql-collation-guard-on-failure ` companion
unit (triggered via the main unit’s ` OnFailure= `) runs as\.
Defaults to match ` services.postgresql.superUser ` (also
“postgres” unless overridden) – the same Postgres superuser
the main unit itself runs as, since this unit’s job is
identical in kind (running the exact same hook mechanism,
plus the crash-recovery lockdown-file cleanup, which connects
to Postgres as this user) – override only for a deployment-
specific reason, e\.g\. a hardened setup that runs ` onFailure `
hooks under a dedicated, more restricted account\.



*Type:*
string



*Default:*

```nix
"postgres"
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



Safety cap on the repair loop for one partitioned table, where
each attempt is a full pass over every leaf partition, repeated
until a pass moves nothing\. Two passes provably suffice given
` connectionLockdown ` excludes every other writer during repair
(the default) – see
docs/learnings/partition-repair-convergence\.md for the full
reasoning\. The default of 3 is that proven bound plus one
pass of margin: hitting the cap is a signal something
unexpected is happening, not evidence the table just needed
more time, so it fails loudly rather than retrying up to some
much larger number\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
3
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.enable



Whether to orchestrate a PostgreSQL major-version upgrade
(` pg_upgrade `) on the next boot, before ` postgresql.service `
starts\. Must be set explicitly – never triggered
automatically just because ` services.postgresql.package `’s
major version differs from what’s on disk, unlike the
always-on collation guard above (see
docs/decisions/0011-upgrade-trigger-and-old-datadir-retention\.md)\.
Idempotent by construction: safe to leave ` true ` indefinitely
across any number of subsequent boots once the upgrade has
completed, since the only gate is whether the *new* data
directory already has its own cluster\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.initdbArgs



Extra arguments passed to the new cluster’s own ` initdb `,
run before ` pg_upgrade ` migrates data into it – mirrors
` services.postgresql.initdbArgs ` so the resulting fresh
cluster matches what a normal first boot under the new
package would have produced\. Not read from
` services.postgresql.initdbArgs ` automatically (that option
is evaluated against the NEW package already, for the normal
non-upgrade path) – set the same value here explicitly if
needed\.



*Type:*
list of string



*Default:*

```nix
[ ]
```



*Example:*

```nix
[
  "--data-checksums"
]
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.jobs



` pg_upgrade `’s own ` --jobs ` – parallelizes per-database
dump/restore and file transfer\. ` null ` leaves it unset
(` pg_upgrade `’s own single-job default)\.



*Type:*
null or (positive integer, meaning >0)



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.oldDataDir



The on-disk data directory for the OLD cluster referenced by
` oldPackage ` above\. Defaults to the same convention
` services.postgresql.dataDir ` itself uses – override only
if the old cluster lives somewhere nonstandard\.



*Type:*
null or absolute path



*Default:*

```nix
"/var/lib/postgresql/${oldPackage.psqlSchema}"
```



*Example:*

```nix
"/var/lib/postgresql/15"
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.oldDataDirRetentionDays



Automatic cleanup of the old data directory after a
successful upgrade\. ` null ` (default) keeps it forever – the
safe default, since ` pg_upgrade `’s own documentation
recommends keeping the old cluster until the new one is
verified in production\. ` 0 ` deletes it immediately once the
upgrade completes\. A positive N deletes it N days later, via
a separate
` postgresql-collation-guard-upgrade-cleanup.timer ` that
checks once a day – independent of any particular boot,
since this is calendar time elapsing, not a repeat upgrade
run\. See
docs/decisions/0011-upgrade-trigger-and-old-datadir-retention\.md\.



*Type:*
null or (unsigned integer, meaning >=0)



*Default:*

```nix
null
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.oldPackage



The PostgreSQL package the on-disk cluster at ` oldDataDir `
is currently running – required whenever ` enable ` is ` true `
(no safe default exists: this is validated directly against
the real on-disk ` PG_VERSION ` before anything irreversible
runs, see
docs/decisions/0010-pg-upgrade-preflight-before-orchestration\.md)\.
If the old cluster has extensions installed (e\.g\. PostGIS),
pass the same ` .withPackages `-wrapped package it was
originally configured with – ` pg_upgrade ` needs their
shared libraries available to briefly start the old cluster\.



*Type:*
package



*Example:*

```nix
pkgs.postgresql_15
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.upgrade\.transferMode



How ` pg_upgrade ` transfers data from the old cluster into
the new one\. ` "auto" ` picks ` "clone" ` (copy-on-write, same
filesystem) when available, falling back to ` "copy" ` (a full
independent duplicate, needs roughly 2x disk space)
otherwise – but never ` "link" ` (hard links: the old
cluster’s files are silently corrupted the moment the new
cluster starts writing, so it only ever runs on an explicit,
deliberate request, never a substitution ` "auto" ` makes on
the caller’s behalf)\. See
docs/decisions/0010-pg-upgrade-preflight-before-orchestration\.md\.



*Type:*
one of “auto”, “copy”, “clone”, “link”



*Default:*

```nix
"auto"
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)


