Name:           pavucli
Version:        0.1.0
Release:        1%{?dist}
Summary:        pavucontrol-like terminal mixer for PulseAudio and PipeWire
License:        MIT
URL:            https://github.com/pwarnimo-bot/pavucli
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
# pactl: provided by pulseaudio-utils (Fedora, openSUSE)
Requires:       /usr/bin/pactl

%description
pavucli is a curses mixer in the spirit of pavucontrol. It controls the
volume, per-channel volume, balance and mute state of playback/recording
streams and devices, moves streams between devices, selects ports, sets the
default device and switches sound card profiles. Works with PulseAudio and
PipeWire (pipewire-pulse).

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files pavucli
install -Dm644 man/pavucli.1 %{buildroot}%{_mandir}/man1/pavucli.1
install -Dm644 data/pavucli.desktop %{buildroot}%{_datadir}/applications/pavucli.desktop

%check
%{python3} -m unittest discover -s tests

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/pavucli
%{_mandir}/man1/pavucli.1*
%{_datadir}/applications/pavucli.desktop

%changelog
* Mon Nov 16 2026 pwarnimont <pwarnimo@gmail.com> - 0.1.0-1
- Initial package
