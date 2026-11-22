' Starts the FreePT server with no window (pythonw). Output goes to server.log next to freept.py.
' Double-click to start; it does nothing visible. Check http://127.0.0.1:4000/ to see it running.
' If the server is already running, the new copy exits right away (port 4000 is taken).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run "pythonw.exe -I """ & here & "\freept.py"" serve", 0, False
