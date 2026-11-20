Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
serverPath = baseDir & "\server.py"
htmlPath = baseDir & "\deepseek网页版.html"
pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"

' 先把工作目录切到项目目录，再用 python.exe 配合窗口样式 0（隐藏窗口）静默启动
shell.CurrentDirectory = baseDir
shell.Run """" & pythonExe & """ """ & serverPath & """", 0, False

' 等 1.5 秒让端口监听就绪，再打开网页
WScript.Sleep 1500
If fso.FileExists(htmlPath) Then
    shell.Run """" & htmlPath & """", 1, False
End If