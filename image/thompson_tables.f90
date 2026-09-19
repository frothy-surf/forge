! Writes the Thompson microphysics lookup tables (qr_acr_qg_V4.dat,
! qr_acr_qsV2.dat, freezeH2O.dat) into the current directory by calling
! WRF's own thompson_init, linked against the libwrflib.a that wrf.exe is
! linked from — the same code, compiler and flags that would otherwise
! compute them at the start of a forecast. See build_thompson_tables.sh.
!
! thompson_init computes a table only when its file is absent and reads it
! otherwise, so the files this leaves behind are what a run picks up. The
! tables are functions of the scheme's constants alone: no grid, namelist
! or input data enters them, which is why a dummy 3x3x3 height field (read
! only by a debug print on this path) is enough.
!
! Called without the aerosol (mp_physics=28) or hail (38) optional
! arguments. 28 reads the same three files; the hail-aware scheme uses a
! different, much larger graupel table that is not built here.
program thompson_tables
  use module_configure, only : model_config_rec
  use module_mp_thompson, only : thompson_init
  implicit none
  real :: hgt(3,3,3)
  character(len=16) :: mode

  ! Phase 1 brings up MPI and the communicator wrf_dm_on_monitor needs
  ! (a no-op in a serial/smpar build); phase 2 the error/timing modules.
  call init_modules(1)
  call init_modules(2)

  ! No namelist is read here, so set the two switches the table routines
  ! consult: compute when absent, then write (the Registry defaults). With
  ! the argument "check", reading becomes mandatory instead — WRF then
  ! aborts on a missing or unreadable table rather than quietly recomputing
  ! it, which is how the build proves the files it made are usable.
  call get_command_argument(1, mode)
  model_config_rec%force_read_thompson = (trim(mode) == 'check')
  model_config_rec%write_thompson_tables = .true.

  hgt = 0.
  call thompson_init(hgt, ids=1, ide=3, jds=1, jde=3, kds=1, kde=3, &
                     ims=1, ime=3, jms=1, jme=3, kms=1, kme=3,      &
                     its=1, ite=2, jts=1, jte=2, kts=1, kte=2)

  call wrf_shutdown
end program thompson_tables
