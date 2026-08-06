@{
    RootModule = 'Cerebro.Tools.psm1'
    ModuleVersion = '1.0.0'
    GUID = '6e1252dc-8979-4c13-a2e0-0de3ce184f65'
    Author = 'Cerebro'
    CompanyName = 'Cerebro'
    Copyright = 'Cerebro'
    Description = 'Local Cerebro transport and tooling module.'
    PowerShellVersion = '5.1'
    FunctionsToExport = @(
        'cerebro_receive',
        'cerebro_sync',
        'cerebro_handoff',
        'cerebro_resume',
        'cerebro_tools_status'
    )
    CmdletsToExport = @()
    VariablesToExport = @()
    AliasesToExport = @()
}
