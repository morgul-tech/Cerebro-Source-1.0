@{
    RootModule = 'Cerebro.Tools.psm1'
    ModuleVersion = '1.1.0'
    GUID = '6e1252dc-8979-4c13-a2e0-0de3ce184f65'
    Author = 'Cerebro'
    CompanyName = 'Cerebro'
    Copyright = 'Cerebro'
    Description = 'Local Cerebro transport and tooling module.'
    PowerShellVersion = '5.1'
    FunctionsToExport = @(
        'cerebro_receive',
        'cpatch',
        'cerebro_sync',
        'cerebro_handoff',
        'cerebro_resume',
        'bootCerebro',
        'bootini',
        'cerebro_tools_status',
        'cerebro_profile'
    )
    CmdletsToExport = @()
    VariablesToExport = @()
    AliasesToExport = @()
}
