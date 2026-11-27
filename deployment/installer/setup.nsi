Unicode true
RequestExecutionLevel user
ManifestDPIAware true
SetCompressor /SOLID lzma
Name "LabControl 石墨版"
OutFile "${OUT}"
Icon "${ICON}"
UninstallIcon "${ICON}"
InstallDir "$LOCALAPPDATA\Programs\LabControl"
VIProductVersion "${WINVERSION}"
VIAddVersionKey "ProductName" "LabControl"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${WINVERSION}"
VIAddVersionKey "FileDescription" "LabControl 石墨版 Windows x64 安装程序"
VIAddVersionKey "LegalCopyright" "LabControl"
!include MUI2.nsh
!include LogicLib.nsh
!include FileFunc.nsh
!include x64.nsh
!include WinVer.nsh
!include "${GENERATED}\remove-files.nsh"
!define REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\LabControl.Graphite"
!define SHORTCUT "LabControl 石墨版.lnk"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
Var Relaunch

Function .onInit
    SetRegView 64
    SetShellVarContext current
    ${IfNot} ${RunningX64}
    ${OrIfNot} ${AtLeastWin10}
        MessageBox MB_OK|MB_ICONSTOP "需要 Windows 10 或更高版本的 64 位系统。" /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    ReadRegStr $0 HKCU "${REGKEY}" "InstallLocation"
    ${If} $0 != ""
        StrCpy $INSTDIR "$0"
    ${EndIf}
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/RELAUNCH" $1
    ${IfNot} ${Errors}
        StrCpy $Relaunch "yes"
    ${EndIf}
    ClearErrors
    ${GetOptions} $0 "/WAITPID=" $1
    ${IfNot} ${Errors}
        System::Call 'kernel32::OpenProcess(i 0x100000,i 0,i r1)p.r2'
        ${If} $2 != 0
            System::Call 'kernel32::WaitForSingleObject(p r2,i 120000)i.r3'
            System::Call 'kernel32::CloseHandle(p r2)'
            ${If} $3 != 0
                MessageBox MB_OK|MB_ICONSTOP "原程序仍在运行，请正常退出后重试。" /SD IDOK
                SetErrorLevel 2
                Abort
            ${EndIf}
        ${EndIf}
    ${EndIf}
FunctionEnd

Section
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=payload.json "${GENERATED}\payload.json"
    File /oname=legacy.json "${LEGACY}"
    ClearErrors
    !include "${GENERATED}\install-files.nsh"
    WriteUninstaller "$PLUGINSDIR\payload\Uninstall.exe"
    IfErrors failed
    SetOutPath "$PLUGINSDIR"
    ExecWait '"$PLUGINSDIR\payload\LabControl.exe" --apply-setup "$PLUGINSDIR\payload" "$INSTDIR" "$SMPROGRAMS" "$PLUGINSDIR"' $0
    IfErrors failed
    IntCmp $0 0 success failed failed
  failed:
    StrCpy $2 "请检查程序占用、安装位置和磁盘空间。"
    ClearErrors
    FileOpen $1 "$PLUGINSDIR\error.txt" r
    IfErrors report
    FileReadUTF16LE $1 $2
    FileClose $1
  report:
    ${If} $Relaunch == "yes"
        System::Call 'user32::MessageBoxW(p 0,w "$2",w "LabControl 更新失败",i 16)'
    ${Else}
        MessageBox MB_OK|MB_ICONSTOP "$2" /SD IDOK
    ${EndIf}
    SetErrorLevel 1
    Abort
  success:
    ${If} $Relaunch == "yes"
        SetOutPath "$INSTDIR"
        ReadEnvStr $1 LABCONTROL_RELAUNCH_CONFIG
        ${If} $1 == ""
            Exec '"$INSTDIR\LabControl.exe"'
        ${Else}
            Exec '"$INSTDIR\LabControl.exe" --config "$1"'
        ${EndIf}
    ${EndIf}
SectionEnd

Function un.onInit
    SetShellVarContext current
    SetRegView 64
    ReadRegStr $0 HKCU "${REGKEY}" "InstallLocation"
    ${If} $0 != $INSTDIR
        SetErrorLevel 2
        Abort
    ${EndIf}
    IfFileExists "$INSTDIR\LabControl.exe" 0 done
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\LabControl.exe",i 0x80000000,i 0,p 0,i 3,i 0,p 0)p.r0'
    ${If} $0 == -1
        MessageBox MB_OK|MB_ICONSTOP "请先结束采集并正常退出 LabControl。" /SD IDOK
        SetErrorLevel 2
        Abort
    ${EndIf}
    System::Call 'kernel32::CloseHandle(p r0)'
  done:
FunctionEnd

Section Uninstall
    SetOutPath "$TEMP"
    !insertmacro RemoveOwnedFiles "$INSTDIR"
    Delete "$SMPROGRAMS\${SHORTCUT}"
    DeleteRegKey HKCU "${REGKEY}"
    Delete "$INSTDIR\.labcontrol-owned.json"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
SectionEnd
