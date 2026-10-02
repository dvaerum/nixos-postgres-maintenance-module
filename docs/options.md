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
connect against inconsistent state\. A database with nothing
to fix is never locked\. Default ` true ` since this closes a
real correctness gap, but every existing deployment gets this
behavior on the next upgrade with no config change – turn it
off here if there’s a specific reason to allow concurrent
connections during the guard’s run\. See
docs/decisions/0007-connection-lockdown-during-repair\.md\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onFailure\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.onSuccess\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onFailure\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.onSuccess\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.perDatabase\.preStart\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.postRun\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)



## services\.postgresqlCollationGuard\.hooks\.preStart\.\*\.environmentFile



` EnvironmentFile `-style ` KEY=VALUE ` file (e\.g\. a sops secret
path), merged with ` environment ` and the stage’s own default
variables under the same no-collision rule\.



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
until a pass moves nothing\. Capped rather than single-pass
because moving a misplaced row into a different partition can
require that partition’s own next pass to re-check it, and
there’s no proof this always converges in one sweep (see
docs/learnings/partition-repair-testing\.md)\. Hitting the cap
fails loudly instead of spinning forever\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
1000
```

*Declared by:*
 - [/home/dennis/nixos-postgres-maintenance-module/nixosModule/options\.nix](file:///home/dennis/nixos-postgres-maintenance-module/nixosModule/options.nix)


