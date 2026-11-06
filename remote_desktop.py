#!/usr/bin/env python3
"""
HyprRemote - Zdalny pulpit na Arch Linux (Hyprland / Wayland)
Pozwala na podgląd i sterowanie laptopem z telefonu przez lokalną sieć Wi-Fi / Hotspot.
Obsługuje działanie przy wygaszonym ekranie laptopa (jasność 0% lub wirtualny ekran headless)
oraz zapobieganie uśpieniu przy zamkniętej klapie laptopa.
"""

import sys
import os
import subprocess
import json
import signal
import time
import shutil
import argparse
import socket
import tempfile

BANNER = r"""
  _    _                  _____                      _       
 | |  | |                |  __ \                    | |      
 | |__| |_   _ _ __  _ __| |__) |___ _ __ ___   ___ | |_ ___ 
 |  __  | | | | '_ \| '__|  _  // _ \ '_ ` _ \ / _ \| __/ _ \
 | |  | | |_| | |_) | |  | | \ \  __/ | | | | | (_) | ||  __/
 |_|  |_|\__, | .__/|_|  |_|  \_\___|_| |_| |_|\___/ \__\___|
          __/ | |                                            
         |___/|_|      Wayland / Hyprland Phone Remote       
"""

COLOR_RESET = "\033[0m"
COLOR_BOLD = "\033[1m"
COLOR_CYAN = "\033[96m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_RED = "\033[91m"
COLOR_MAGENTA = "\033[95m"

def print_header():
    print(COLOR_CYAN + BANNER + COLOR_RESET)
    print(f"{COLOR_BOLD}Sterowanie laptopem z telefonu w sieci lokalnej (Hyprland / Wayland){COLOR_RESET}\n")

def check_environment():
    """Sprawdza środowisko graficzne Wayland / Hyprland."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    
    if "wayland" not in session_type and "hyprland" not in desktop:
        print(f"{COLOR_YELLOW}[!] Ostrzeżenie:{COLOR_RESET} Bieżąca sesja nie wygląda na Wayland ({session_type} / {desktop}).")
        print("    Program został zoptymalizowany pod Hyprland / Wayland z użyciem wayvnc.\n")

def check_dependencies():
    """Sprawdza obecność wayvnc i qrencode."""
    has_wayvnc = shutil.which("wayvnc") is not None
    has_qrencode = shutil.which("qrencode") is not None
    return has_wayvnc, has_qrencode

def install_dependencies():
    """Proponuje instalację brakujących zależności."""
    print(f"\n{COLOR_YELLOW}[*] Instalacja brakujących pakietów (wayvnc, qrencode)...{COLOR_RESET}")
    cmd = ["sudo", "pacman", "-S", "--needed", "wayvnc", "qrencode"]
    try:
        res = subprocess.run(cmd)
        return res.returncode == 0
    except Exception as e:
        print(f"{COLOR_RED}[-] Błąd podczas instalacji: {e}{COLOR_RESET}")
        return False

def get_network_ips():
    """Wykrywa lokalne adresy IP (Wi-Fi, LAN, Hotspot)."""
    ips = []
    
    # Metoda 1: ip -j addr
    try:
        res = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for iface in data:
                ifname = iface.get("ifname", "")
                if ifname.startswith("lo") or ifname.startswith("docker") or ifname.startswith("veth"):
                    continue
                for addr in iface.get("addr_info", []):
                    if addr.get("family") == "inet":
                        local_ip = addr.get("local")
                        if local_ip and not local_ip.startswith("127."):
                            ips.append((ifname, local_ip))
    except Exception:
        pass

    # Metoda 2: hostname -I
    if not ips:
        try:
            res = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
            if res.returncode == 0:
                for ip in res.stdout.strip().split():
                    if ip and not ip.startswith("127."):
                        ips.append(("sieć", ip))
        except Exception:
            pass

    # Metoda 3: UDP socket probe
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("1.1.1.1", 80))
            probe_ip = s.getsockname()[0]
            s.close()
            if probe_ip and not probe_ip.startswith("127."):
                ips.append(("domyślny", probe_ip))
        except Exception:
            pass

    return ips

def detect_monitors():
    """Wykrywa aktywne monitory w Hyprland / systemie."""
    monitors = []
    
    # 1. hyprctl -j monitors
    try:
        res = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            for m in data:
                name = m.get("name")
                desc = m.get("description", "")
                w = m.get("width", 0)
                h = m.get("height", 0)
                scale = m.get("scale", 1.0)
                focused = m.get("focused", False)
                monitors.append({
                    "name": name,
                    "desc": f"{desc} ({w}x{h} @ scale {scale})",
                    "focused": focused
                })
    except Exception:
        pass

    # 2. Sysfs DRM fallback
    if not monitors:
        drm_path = "/sys/class/drm"
        if os.path.exists(drm_path):
            try:
                for entry in os.listdir(drm_path):
                    status_file = os.path.join(drm_path, entry, "status")
                    if os.path.isfile(status_file):
                        with open(status_file, "r") as f:
                            status = f.read().strip()
                        if status == "connected":
                            # e.g. card1-eDP-1 -> eDP-1
                            parts = entry.split("-", 1)
                            name = parts[1] if len(parts) > 1 else entry
                            monitors.append({
                                "name": name,
                                "desc": f"Podłączony port graficzny ({name})",
                                "focused": "eDP" in name
                            })
            except Exception:
                pass

    # 3. Parsowanie ~/.config/hypr/hyprland.conf
    if not monitors:
        conf_path = os.path.expanduser("~/.config/hypr/hyprland.conf")
        if os.path.exists(conf_path):
            try:
                with open(conf_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("monitor") and "=" in line:
                            rest = line.split("=", 1)[1].strip()
                            m_name = rest.split(",")[0].strip()
                            if m_name and m_name != "":
                                monitors.append({
                                    "name": m_name,
                                    "desc": f"Zdefiniowany w hyprland.conf ({m_name})",
                                    "focused": "eDP" in m_name
                                })
            except Exception:
                pass

    # 4. Ostateczny domyślny fallback
    if not monitors:
        monitors.append({
            "name": "eDP-1",
            "desc": "Domyślny wbudowany ekran laptopa",
            "focused": True
        })

    return monitors

def print_qr_code(text):
    """Generuje kod QR w terminalu za pomocą qrencode."""
    if shutil.which("qrencode"):
        try:
            print(f"\n{COLOR_BOLD}Zeskanuj telefonem, aby odczytać adres połączenia:{COLOR_RESET}")
            res = subprocess.run(["qrencode", "-t", "ANSIUTF8", text], capture_output=True, text=True)
            if res.returncode == 0:
                print(res.stdout)
                return True
        except Exception:
            pass
    return False

def get_brightness():
    """Pobiera aktualną jasność ekranu przez brightnessctl."""
    if shutil.which("brightnessctl"):
        try:
            res = subprocess.run(["brightnessctl", "get"], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    return None

def set_brightness(val):
    """Ustawia jasność ekranu przez brightnessctl."""
    if shutil.which("brightnessctl") and val is not None:
        try:
            subprocess.run(["brightnessctl", "set", str(val)], capture_output=True)
            return True
        except Exception:
            pass
    return False

def create_headless_output():
    """Tworzy wirtualny monitor w Hyprland."""
    try:
        res = subprocess.run(["hyprctl", "output", "create", "headless"], capture_output=True, text=True)
        if res.returncode == 0:
            # Domyślnie Hyprland tworzy HEADLESS-1 lub HEADLESS-2
            # Szukamy nowo utworzonego wyjścia
            time.sleep(0.5)
            monitors = detect_monitors()
            for m in reversed(monitors):
                if "HEADLESS" in m["name"].upper():
                    return m["name"]
            return "HEADLESS-1"
    except Exception:
        pass
    return None

def remove_headless_output(name):
    """Usuwa wirtualny monitor w Hyprland."""
    try:
        subprocess.run(["hyprctl", "output", "remove", name], capture_output=True)
    except Exception:
        pass

def set_dpms(monitor, state):
    """Włącza lub wyłącza DPMS dla monitora w Hyprland (on / off)."""
    try:
        subprocess.run(["hyprctl", "dispatch", "dpms", state, monitor], capture_output=True)
    except Exception:
        pass

def run_server(monitor_name, port=5900, password=None, blank_screen=False, prevent_sleep=False, headless=False):
    """Uruchamia serwer wayvnc z opcjonalnym wygaszaniem ekranu i blokadą uśpienia."""
    created_headless = None
    original_brightness = None

    # Obsługa trybu Headless (wirtualnego ekranu)
    if headless:
        print(f"\n{COLOR_YELLOW}[*] Tworzenie wirtualnego monitora Headless w Hyprland...{COLOR_RESET}")
        created_headless = create_headless_output()
        if created_headless:
            print(f"{COLOR_GREEN}[✓] Utworzono wirtualny monitor: {created_headless}{COLOR_RESET}")
            # Ustawienie rozdzielczości
            subprocess.run(["hyprctl", "keyword", "monitor", f"{created_headless}, 1920x1080@60, auto, 1"], capture_output=True)
            monitor_name = created_headless
            if blank_screen:
                print(f"{COLOR_YELLOW}[*] Wyłączanie fizycznego ekranu eDP-1 (DPMS off)...{COLOR_RESET}")
                set_dpms("eDP-1", "off")
        else:
            print(f"{COLOR_RED}[-] Nie udało się utworzyć ekranu headless. Kontynuacja z fizycznym monitorem.{COLOR_RESET}")

    # Obsługa trybu Blank Screen (Jasność 0%)
    if blank_screen and not headless:
        original_brightness = get_brightness()
        if original_brightness:
            print(f"\n{COLOR_YELLOW}[*] Tryb czarnego ekranu (Jasność 0%):{COLOR_RESET} Ekran laptopa zostanie wygaszony po uruchomieniu.")
            print(f"    (Oryginalna jasność: {original_brightness} zostanie automatycznie przywrócona po zakończeniu).")
            set_brightness(0)

    print(f"\n{COLOR_GREEN}{'='*60}{COLOR_RESET}")
    print(f"{COLOR_BOLD}{COLOR_GREEN} Uruchamianie serwera zdalnego pulpitu (WayVNC)...{COLOR_RESET}")
    print(f"{COLOR_GREEN}{'='*60}{COLOR_RESET}")
    print(f"  • Wybrany monitor:    {COLOR_BOLD}{monitor_name}{COLOR_RESET}")
    print(f"  • Port VNC:           {COLOR_BOLD}{port}{COLOR_RESET}")
    print(f"  • Nasłuch:            {COLOR_BOLD}0.0.0.0 (wszystkie sieci lokalne){COLOR_RESET}")
    print(f"  • Wygaszony ekran:    {COLOR_BOLD}{'TAK (jasność 0%)' if blank_screen and not headless else ('TAK (Headless + DPMS off)' if headless else 'NIE (normalny)')}{COLOR_RESET}")
    print(f"  • Blokada uśpienia:   {COLOR_BOLD}{'TAK (systemd-inhibit włączony)' if prevent_sleep else 'Domyślna'}{COLOR_RESET}")
    
    ips = get_network_ips()
    print(f"\n{COLOR_BOLD}Adresy Twojego laptopa w sieci lokalnej:{COLOR_RESET}")
    if ips:
        for iface, ip in ips:
            print(f"  {COLOR_CYAN}➜ [{iface}]{COLOR_RESET}  Adres VNC:  {COLOR_BOLD}{COLOR_GREEN}{ip}:{port}{COLOR_RESET}")
    else:
        print(f"  {COLOR_YELLOW}[!] Nie wykryto aktywnego IP. Upewnij się, że laptop i telefon są w tej samej sieci Wi-Fi lub Hotspocie.{COLOR_RESET}")
        print(f"  Domyślny port do wpisania w telefonie: {port}")

    # Kod QR dla pierwszego IP
    if ips:
        vnc_uri = f"vnc://{ips[0][1]}:{port}"
        print_qr_code(vnc_uri)

    # Przygotowanie konfiguracji z hasłem lub bez
    temp_config = None
    cmd = []
    
    # Obsługa zapobiegania uśpieniu przy zamkniętej klapie laptopa
    if prevent_sleep and shutil.which("systemd-inhibit"):
        cmd.extend([
            "systemd-inhibit",
            "--what=handle-lid-switch:sleep",
            "--who=HyprRemote",
            "--why=Zdalny pulpit aktywny z telefonu",
            "wayvnc"
        ])
    else:
        cmd.append("wayvnc")
    
    if monitor_name:
        cmd.extend(["-o", monitor_name])
        
    if password:
        temp_config = tempfile.NamedTemporaryFile(mode="w", delete=False, prefix="wayvnc_", suffix=".conf")
        temp_config.write(f"[auth]\nusername=hypr\npassword={password}\n")
        temp_config.close()
        cmd.extend(["-c", temp_config.name])
        print(f"\n{COLOR_YELLOW}Uwierzytelnianie:{COLOR_RESET} Włączone")
        print(f"  • Użytkownik: {COLOR_BOLD}hypr{COLOR_RESET}")
        print(f"  • Hasło:      {COLOR_BOLD}{password}{COLOR_RESET}")
    else:
        print(f"\n{COLOR_YELLOW}Uwierzytelnianie:{COLOR_RESET} {COLOR_GREEN}Bezhasłowe{COLOR_RESET} (zaufana sieć lokalna)")

    cmd.extend(["0.0.0.0", str(port)])

    print(f"\n{COLOR_BOLD}Jak połączyć się z telefonu:{COLOR_RESET}")
    print(f"  1. Upewnij się, że telefon i laptop są w tym samym Wi-Fi (lub włącz Hotspot w telefonie).")
    print(f"  2. Na telefonie otwórz klienta VNC:")
    print(f"     • {COLOR_BOLD}Android:{COLOR_RESET} darmowa aplikacja {COLOR_CYAN}AVNC{COLOR_RESET} (Google Play / F-Droid)")
    print(f"     • {COLOR_BOLD}iPhone/iPad:{COLOR_RESET} darmowa aplikacja {COLOR_CYAN}VNC Viewer{COLOR_RESET} (RealVNC)")
    print(f"  3. Wpisz adres: {COLOR_BOLD}{ips[0][1] if ips else '192.168.x.x'}:{port}{COLOR_RESET}")
    if prevent_sleep:
        print(f"  {COLOR_GREEN}★ Możesz teraz zamknąć klapę laptopa bez obawy o uśpienie!{COLOR_RESET}")
    print(f"\n{COLOR_MAGENTA}Naciśnij Ctrl+C w tym terminalu, aby zakończyć sesję.{COLOR_RESET}\n")

    # Uruchomienie wayvnc
    proc = None
    try:
        proc = subprocess.Popen(cmd)
        proc.wait()
    except KeyboardInterrupt:
        print(f"\n\n{COLOR_YELLOW}[*] Zamykanie serwera zdalnego pulpitu...{COLOR_RESET}")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        
        # Przywrócenie jasności ekranu
        if original_brightness:
            print(f"{COLOR_YELLOW}[*] Przywracanie jasności ekranu laptopa...{COLOR_RESET}")
            set_brightness(original_brightness)
            
        # Sprzątanie headless / przywracanie DPMS
        if created_headless:
            print(f"{COLOR_YELLOW}[*] Usuwanie wirtualnego ekranu i włączanie fizycznego ekranu...{COLOR_RESET}")
            set_dpms("eDP-1", "on")
            remove_headless_output(created_headless)

        if temp_config and os.path.exists(temp_config.name):
            try:
                os.remove(temp_config.name)
            except Exception:
                pass
        print(f"{COLOR_GREEN}[✓] Serwer zatrzymany. Do widzenia!{COLOR_RESET}\n")

def interactive_menu():
    """Menu główne programu."""
    print_header()
    check_environment()
    
    has_wayvnc, has_qrencode = check_dependencies()
    if not has_wayvnc:
        print(f"{COLOR_RED}[!] Brak programu 'wayvnc' w systemie.{COLOR_RESET}")
        print("    'wayvnc' jest niezbędny do przechwytywania ekranu i sterowania w Wayland/Hyprland.")
        choice = input(f"\nCzy chcesz zainstalować wymagane pakiety (sudo pacman -S wayvnc qrencode)? [T/n]: ").strip().lower()
        if choice in ("", "t", "tak", "y", "yes"):
            if not install_dependencies():
                print(f"{COLOR_RED}[-] Instalacja nie powiodła się. Zainstaluj ręcznie: sudo pacman -S wayvnc qrencode{COLOR_RESET}")
                sys.exit(1)
            has_wayvnc, has_qrencode = check_dependencies()
        else:
            print(f"{COLOR_RED}[-] Nie można kontynuować bez 'wayvnc'.{COLOR_RESET}")
            sys.exit(1)

    monitors = detect_monitors()
    default_monitor = monitors[0]["name"]
    for m in monitors:
        if m.get("focused") or "eDP" in m.get("name", ""):
            default_monitor = m["name"]
            break

    selected_monitor = default_monitor
    port = 5900
    password = None
    blank_screen = False
    prevent_sleep = True
    headless = False

    while True:
        status_blank = f"{COLOR_GREEN}WŁĄCZONE (jasność 0%){COLOR_RESET}" if blank_screen else f"{COLOR_YELLOW}WYŁĄCZONE{COLOR_RESET}"
        status_sleep = f"{COLOR_GREEN}WŁĄCZONE (nie uśnie po zamknięciu klapy){COLOR_RESET}" if prevent_sleep else f"{COLOR_YELLOW}WYŁĄCZONE{COLOR_RESET}"
        status_headless = f"{COLOR_GREEN}WŁĄCZONE (wirtualny monitor){COLOR_RESET}" if headless else f"{COLOR_YELLOW}WYŁĄCZONE (ekran fizyczny){COLOR_RESET}"

        print(f"{COLOR_BOLD}Konfiguracja połączenia:{COLOR_RESET}")
        print(f"  [1] {COLOR_GREEN}Uruchom serwer zdalnego pulpitu{COLOR_RESET} (Monitor: {COLOR_BOLD}{selected_monitor if not headless else 'HEADLESS'}{COLOR_RESET}, Port: {COLOR_BOLD}{port}{COLOR_RESET})")
        print(f"  [2] Zmień monitor do udostępniania (Aktualny: {COLOR_CYAN}{selected_monitor}{COLOR_RESET})")
        print(f"  [3] Zmień port (Aktualny: {COLOR_CYAN}{port}{COLOR_RESET})")
        print(f"  [4] Zabezpieczenie hasłem (Aktualny: {COLOR_CYAN}{'Włączone' if password else 'Wyłączone (bezhasłowo)'}{COLOR_RESET})")
        print(f"  [5] Przełącz wygaszanie ekranu laptopa (Jasność 0%): {status_blank}")
        print(f"  [6] Blokada uśpienia przy zamknięciu klapy: {status_sleep}")
        print(f"  [7] Tryb wirtualnego monitora (Headless): {status_headless}")
        print(f"  [8] Informacje o alternatywach (Sunshine + Moonlight, Weylus)")
        print(f"  [0] Wyjście")
        
        try:
            choice = input(f"\nWybierz opcję [domyślnie 1]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nWyjście.")
            break

        if choice in ("", "1"):
            run_server(selected_monitor, port, password, blank_screen, prevent_sleep, headless)
            break
        elif choice == "2":
            print(f"\n{COLOR_BOLD}Dostępne monitory:{COLOR_RESET}")
            for idx, m in enumerate(monitors):
                prefix = f"  [{idx + 1}]"
                print(f"{prefix} {COLOR_BOLD}{m['name']}{COLOR_RESET} - {m['desc']}")
            print(f"  [{len(monitors) + 1}] Wpisz własną nazwę monitora")
            m_choice = input(f"Wybierz monitor [1-{len(monitors) + 1}]: ").strip()
            if m_choice.isdigit():
                m_idx = int(m_choice) - 1
                if 0 <= m_idx < len(monitors):
                    selected_monitor = monitors[m_idx]["name"]
                    print(f"{COLOR_GREEN}[✓] Wybrano monitor: {selected_monitor}{COLOR_RESET}\n")
                elif m_idx == len(monitors):
                    custom_name = input("Podaj nazwę wyjścia (np. eDP-1, HDMI-A-1): ").strip()
                    if custom_name:
                        selected_monitor = custom_name
                        print(f"{COLOR_GREEN}[✓] Wybrano monitor: {selected_monitor}{COLOR_RESET}\n")
        elif choice == "3":
            new_port = input(f"Podaj nowy port [1-65535, domyślnie {port}]: ").strip()
            if new_port.isdigit() and 1 <= int(new_port) <= 65535:
                port = int(new_port)
                print(f"{COLOR_GREEN}[✓] Zmieniono port na: {port}{COLOR_RESET}\n")
        elif choice == "4":
            print("\nUstawienia hasła:")
            print("  [1] Bez hasła (najwygodniejsze w domowym Wi-Fi lub Hotspocie)")
            print("  [2] Wygeneruj losowy 6-cyfrowy kod PIN")
            print("  [3] Wpisz własne hasło")
            p_choice = input("Wybierz opcję: ").strip()
            if p_choice == "1":
                password = None
                print(f"{COLOR_GREEN}[✓] Wyłączono hasło.{COLOR_RESET}\n")
            elif p_choice == "2":
                import random
                password = str(random.randint(100000, 999999))
                print(f"{COLOR_GREEN}[✓] Wygenerowano kod PIN: {password}{COLOR_RESET}\n")
            elif p_choice == "3":
                custom_pass = input("Wpisz hasło: ").strip()
                if custom_pass:
                    password = custom_pass
                    print(f"{COLOR_GREEN}[✓] Ustawiono hasło.{COLOR_RESET}\n")
        elif choice == "5":
            blank_screen = not blank_screen
            print(f"{COLOR_GREEN}[✓] Wygaszanie ekranu laptopa: {'WŁĄCZONE' if blank_screen else 'WYŁĄCZONE'}{COLOR_RESET}\n")
        elif choice == "6":
            prevent_sleep = not prevent_sleep
            print(f"{COLOR_GREEN}[✓] Blokada uśpienia przy zamknięciu klapy: {'WŁĄCZONA' if prevent_sleep else 'WYŁĄCZONA'}{COLOR_RESET}\n")
        elif choice == "7":
            headless = not headless
            print(f"{COLOR_GREEN}[✓] Tryb wirtualnego monitora Headless: {'WŁĄCZONY' if headless else 'WYŁĄCZONY'}{COLOR_RESET}\n")
        elif choice == "8":
            show_alternatives_info()
        elif choice == "0":
            print("Do widzenia!")
            break
        else:
            print(f"{COLOR_RED}Nieprawidłowy wybór.{COLOR_RESET}\n")

def show_alternatives_info():
    """Prezentuje porównanie i alternatywne opcje."""
    print(f"\n{COLOR_CYAN}{'='*60}{COLOR_RESET}")
    print(f"{COLOR_BOLD}Alternatywne rozwiązania dla Arch Linux + Hyprland:{COLOR_RESET}")
    print(f"{COLOR_CYAN}{'='*60}{COLOR_RESET}")
    print(f"""
{COLOR_BOLD}1. WayVNC (Obecny, wbudowany program){COLOR_RESET}
   • Najprostszy, lekki serwer VNC dedykowany dla Wayland / Hyprland.
   • Idealny do pracy na pulpicie, sterowania oknami, pisania, przeglądania.
   • Na telefonie: aplikacja {COLOR_GREEN}AVNC{COLOR_RESET} (Android) lub {COLOR_GREEN}VNC Viewer{COLOR_RESET} (iOS).

{COLOR_BOLD}2. Sunshine + Moonlight (Dla gier, filmów i 60 FPS){COLOR_RESET}
   • Sunshine to serwer GameStream dla Linuksa (kodowanie sprzętowe NVENC/VAAPI).
   • Moonlight to klient na telefon – oferuje krystaliczny obraz 60 FPS, dźwięk i wirtualny pad/touchpad.
   • Instalacja na Archu: {COLOR_YELLOW}yay -S sunshine{COLOR_RESET}
   • Konfiguracja: przez przeglądarkę pod adresem https://localhost:47990

{COLOR_BOLD}3. Weylus (Dostęp przez przeglądarkę WWW){COLOR_RESET}
   • Zamienia telefon w ekran dotykowy/tablet graficzny bez instalowania aplikacji na telefonie.
   • Instalacja: {COLOR_YELLOW}yay -S weylus-bin{COLOR_RESET}

{COLOR_BOLD}4. KDE Connect (Pilot do laptopa bez przesyłania wideo){COLOR_RESET}
   • Jeśli laptop stoi obok i widzisz jego ekran, a telefon ma służyć jako bezprzewodowy touchpad i klawiatura.
   • Instalacja: {COLOR_YELLOW}sudo pacman -S kdeconnect{COLOR_RESET}
""")
    input(f"{COLOR_BOLD}Naciśnij Enter, aby wrócić do menu...{COLOR_RESET}")
    print()

def main():
    parser = argparse.ArgumentParser(description="HyprRemote - Zdalny pulpit z telefonu dla Hyprland / Arch Linux")
    parser.add_argument("--quick", "-q", action="store_true", help="Uruchom natychmiast z domyślnym monitorem bez pytania")
    parser.add_argument("--blank", "-b", action="store_true", help="Wygaszenie ekranu laptopa (jasność 0%%) podczas sesji")
    parser.add_argument("--headless", action="store_true", help="Uruchom w trybie wirtualnego monitora (fizyczny ekran może być wyłączony)")
    parser.add_argument("--no-sleep", "-s", action="store_true", default=True, help="Zapobiegaj uśpieniu przy zamknięciu klapy (systemd-inhibit)")
    parser.add_argument("--monitor", "-m", help="Nazwa monitora (np. eDP-1, HDMI-A-1)")
    parser.add_argument("--port", "-p", type=int, default=5900, help="Port VNC (domyślnie: 5900)")
    parser.add_argument("--password", help="Hasło VNC")
    
    args = parser.parse_args()

    if args.quick or args.monitor or args.blank or args.headless:
        has_wayvnc, _ = check_dependencies()
        if not has_wayvnc:
            print(f"{COLOR_RED}[!] Brak programu 'wayvnc'. Zainstaluj go: sudo pacman -S wayvnc qrencode{COLOR_RESET}")
            sys.exit(1)
        
        monitor = args.monitor
        if not monitor and not args.headless:
            monitors = detect_monitors()
            monitor = monitors[0]["name"]
        run_server(monitor, args.port, args.password, args.blank, args.no_sleep, args.headless)
    else:
        interactive_menu()

if __name__ == "__main__":
    main()
