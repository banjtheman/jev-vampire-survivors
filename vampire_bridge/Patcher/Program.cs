// Patch only a private runtime copy. No game assembly is distributed by this project.
using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length != 3)
    throw new ArgumentException("Usage: Patcher original-runtime.dll output-runtime.dll bridge.dll");
if (Path.GetFullPath(args[0]) == Path.GetFullPath(args[1]))
    throw new ArgumentException("Output must differ from the original game assembly.");
var resolver = new DefaultAssemblyResolver();
resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(args[0]))!);
resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(args[2]))!);
using var game = AssemblyDefinition.ReadAssembly(args[0], new ReaderParameters { AssemblyResolver = resolver });
using var bridge = AssemblyDefinition.ReadAssembly(args[2], new ReaderParameters { AssemblyResolver = resolver });
if (game.MainModule.AssemblyReferences.Any(r => r.Name == "JevVampireBridge"))
    throw new InvalidOperationException("Input is already patched. Use the original installed assembly.");
var entry = bridge.MainModule.Types.Single(t => t.FullName == "JevVampire.Bridge");
var bootstrap = game.MainModule.ImportReference(entry.Methods.Single(m => m.Name == "Bootstrap" && m.IsStatic));
var afterInput = game.MainModule.ImportReference(entry.Methods.Single(m => m.Name == "AfterInput" && m.IsStatic));
var menu = game.MainModule.Types.Single(t => t.FullName == "VampireSurvivors.UI.MainMenuPage");
var awake = menu.Methods.Single(m => m.Name == "Awake" && m.Parameters.Count == 0 && m.HasBody);
WidenBranches(awake);
awake.Body.GetILProcessor().InsertBefore(awake.Body.Instructions[0], Instruction.Create(OpCodes.Call, bootstrap));
var character = game.MainModule.Types.Single(t => t.FullName == "VampireSurvivors.Objects.Characters.CharacterController");
var input = character.Methods.Single(m => m.Name == "HandlePlayerInput" && m.Parameters.Count == 0 && m.HasBody);
WidenBranches(input);
var returns = input.Body.Instructions.Where(i => i.OpCode == OpCodes.Ret).ToArray();
if (returns.Length == 0) throw new InvalidOperationException("Input handler has no normal return.");
foreach (var ret in returns)
{
    // Keep the instruction itself so branches to the old return also invoke the hook.
    ret.OpCode = OpCodes.Ldarg_0;
    var call = Instruction.Create(OpCodes.Call, afterInput);
    input.Body.GetILProcessor().InsertAfter(ret, call);
    input.Body.GetILProcessor().InsertAfter(call, Instruction.Create(OpCodes.Ret));
}
Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1]))!);
game.Write(args[1]);
Console.WriteLine($"Patched private copy: bootstrap + {returns.Length} input return(s).");

static void WidenBranches(MethodDefinition method)
{
    var longForms = typeof(OpCodes).GetFields(System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Static)
        .Where(f => f.FieldType == typeof(OpCode)).Select(f => (OpCode)f.GetValue(null)!)
        .Where(op => op.OperandType == OperandType.InlineBrTarget).ToDictionary(op => op.Name);
    foreach (var instruction in method.Body.Instructions)
        if (instruction.OpCode.OperandType == OperandType.ShortInlineBrTarget)
            instruction.OpCode = longForms[instruction.OpCode.Name[..^2]];
}
