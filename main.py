import os
import sys
from groq import Groq
from github import Github, Auth
import git
from dotenv import load_dotenv
import tempfile
import json
import re

# Load environment variables
load_dotenv()

# Check if debug mode is enabled
DEBUG_MODE = '--debug' in sys.argv

def debug_print(message):
    """Print message only in debug mode"""
    if DEBUG_MODE:
        print(message)

class RemediationEngine:
    def __init__(self):
        """Initialize Groq and GitHub clients"""
        self.groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
        self.github_client = Github(auth=auth)
        
    def analyze_code(self, file_content, filename):
        """Use Groq LLM to analyze code and find bugs"""
        print(f"\n🔍 Analyzing {filename} with Groq AI...")
        
        prompt = f"""You must respond with ONLY a JSON object, nothing else. No explanations, no markdown, no code blocks.

Analyze this code and respond with valid JSON:

{file_content}

Your response must be EXACTLY this format with no additional text:
{{"has_issues": true, "issues_found": ["specific bug description 1", "specific bug description 2"], "fixed_code": "the complete corrected code with all fixes applied"}}

Remember: 
- Return ONLY the JSON object
- No markdown formatting
- No explanations before or after
- The fixed_code must contain the ENTIRE corrected file
- Be specific about bugs found"""

        try:
            response = self.groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a code analysis bot. You ONLY respond with valid JSON. Never use markdown or explanations."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                model="llama-3.1-8b-instant",
                temperature=0,
                max_tokens=3000,
                response_format={"type": "json_object"}
            )
            
            return response.choices[0].message.content
            
        except Exception as e:
            print(f"❌ Error calling Groq API: {e}")
            return None
    
    def parse_llm_response(self, response_text):
        """Parse JSON from LLM response, handling markdown code blocks"""
        triple_backtick = chr(96) * 3
        
        debug_print(f"\n📝 Raw LLM Response (first 300 chars):")
        debug_print(f"{response_text[:300]}...")
        
        if triple_backtick + 'json' in response_text:
            start = response_text.find(triple_backtick + 'json') + 7
            end = response_text.find(triple_backtick, start)
            json_str = response_text[start:end].strip()
        elif triple_backtick in response_text:
            start = response_text.find(triple_backtick) + 3
            end = response_text.find(triple_backtick, start)
            json_str = response_text[start:end].strip()
        else:
            json_str = response_text.strip()
        
        try:
            result = json.loads(json_str)
            debug_print(f"✅ JSON parsed successfully")
        except json.JSONDecodeError as e:
            debug_print(f"⚠️ JSON parse failed, using manual parser: {e}")
            result = {}
            
            if 'true' in json_str.lower():
                result['has_issues'] = True
            else:
                result['has_issues'] = False
            
            issues_start = json_str.find('"issues_found"')
            if issues_start != -1:
                array_start = json_str.find('[', issues_start)
                array_end = json_str.find(']', array_start)
                issues_text = json_str[array_start+1:array_end]
                issues = []
                current = 0
                while True:
                    q1 = issues_text.find('"', current)
                    if q1 == -1:
                        break
                    q2 = issues_text.find('"', q1+1)
                    if q2 == -1:
                        break
                    issues.append(issues_text[q1+1:q2])
                    current = q2 + 1
                result['issues_found'] = issues
            
            code_start = json_str.find('"fixed_code"')
            if code_start != -1:
                quote_start = json_str.find('"', code_start + 13) + 1
                closing_brace = json_str.rfind('}')
                quote_end = json_str.rfind('"', quote_start, closing_brace)
                if quote_start < quote_end:
                    fixed_code = json_str[quote_start:quote_end]
                    fixed_code = fixed_code.replace('\\n', '\n')
                    fixed_code = fixed_code.replace('\\"', '"')
                    fixed_code = fixed_code.replace('\\\\', '\\')
                    result['fixed_code'] = fixed_code.strip()
        
        if 'fixed_code' in result:
            code = result['fixed_code']
            if triple_backtick + 'python' in code:
                start = code.find(triple_backtick + 'python') + 9
                end = code.find(triple_backtick, start)
                if end != -1:
                    result['fixed_code'] = code[start:end].strip()
            elif triple_backtick in code:
                start = code.find(triple_backtick) + 3
                end = code.find(triple_backtick, start)
                if end != -1:
                    result['fixed_code'] = code[start:end].strip()
        
        debug_print(f"\n📋 Parsed Result:")
        debug_print(f"   - has_issues: {result.get('has_issues', 'N/A')}")
        debug_print(f"   - issues_found: {len(result.get('issues_found', []))} issues")
        debug_print(f"   - fixed_code length: {len(result.get('fixed_code', ''))} chars")
        
        if 'fixed_code' not in result or not result.get('fixed_code'):
            debug_print(f"\n⚠️ WARNING: No fixed_code found!")
            debug_print(f"   Available keys: {list(result.keys())}")
            if 'issues_found' in result:
                debug_print(f"   Issues: {result['issues_found'][:3]}")
        
        return result
    
    def validate_fixed_code(self, original_code, fixed_code, filename):
        """Validate that fixed code is actually valid and not placeholder"""
        debug_print(f"\n🔍 Validating fixed code...")
        
        # Check for common placeholder patterns
        placeholders = [
            "corrected code here",
            "fixed code here",
            "code here",
            "your code here",
            "insert code",
            "# TODO"
        ]
        
        fixed_lower = fixed_code.lower()
        for placeholder in placeholders:
            if placeholder in fixed_lower and placeholder not in original_code.lower():
                print(f"⚠️ Warning: Fixed code contains placeholder: '{placeholder}'")
                return False
        
        # Check if fixed code is too short (less than 50% of original)
        if len(fixed_code) < len(original_code) * 0.5:
            print(f"⚠️ Warning: Fixed code is suspiciously short ({len(fixed_code)} vs {len(original_code)} chars)")
            return False
        
        # Check if it's actually Python code (basic syntax check)
        try:
            compile(fixed_code, filename, 'exec')
            debug_print(f"✅ Fixed code passes Python syntax validation")
            return True
        except SyntaxError as e:
            print(f"⚠️ Warning: Fixed code has syntax errors: {e}")
            return False
    
    def clone_and_fix(self, repo_url, file_path, base_branch="main"):
        """Clone repo, apply fix, create branch and PR"""
        print(f"\n🚀 Starting remediation for: {repo_url}")
        
        repo_url = repo_url.rstrip('/')
        repo_url = repo_url.replace('.git', '')
        repo_parts = repo_url.replace("https://github.com/", "").split("/")
        
        if len(repo_parts) < 2:
            print("❌ Invalid GitHub URL format")
            return
        
        owner, repo_name = repo_parts[0], repo_parts[1]
        
        try:
            print(f"📦 Accessing repository: {owner}/{repo_name}")
            repo = self.github_client.get_repo(f"{owner}/{repo_name}")
            
            print(f"📥 Cloning repository...")
            with tempfile.TemporaryDirectory() as temp_dir:
                cloned_repo = git.Repo.clone_from(f"https://github.com/{owner}/{repo_name}", temp_dir)
                
                file_full_path = os.path.join(temp_dir, file_path)
                
                if not os.path.exists(file_full_path):
                    print(f"❌ File not found: {file_path}")
                    return
                
                with open(file_full_path, 'r', encoding='utf-8') as f:
                    original_content = f.read()
                
                print(f"✅ File loaded: {file_path} ({len(original_content)} characters)")
                
                analysis = self.analyze_code(original_content, file_path)
                
                if not analysis:
                    print("❌ Failed to get analysis from Groq")
                    return
                
                try:
                    result = self.parse_llm_response(analysis)
                except Exception as e:
                    print(f"❌ Failed to parse LLM response: {e}")
                    if DEBUG_MODE:
                        import traceback
                        traceback.print_exc()
                    return
                
                if not result.get("has_issues", False):
                    print("✅ No issues found! Code looks good.")
                    return
                
                issues = result.get("issues_found", [])
                fixed_code = result.get("fixed_code", "")
                
                if not fixed_code:
                    print("❌ LLM did not provide fixed code")
                    return
                
                # Validate fixed code
                if not self.validate_fixed_code(original_content, fixed_code, file_path):
                    print("❌ Fixed code validation failed. Aborting.")
                    if DEBUG_MODE:
                        print(f"\n📄 Fixed code preview (first 500 chars):")
                        print(fixed_code[:500])
                    return
                
                print(f"\n🐛 Issues found:")
                for i, issue in enumerate(issues, 1):
                    print(f"   {i}. {issue}")
                
                with open(file_full_path, 'w', encoding='utf-8') as f:
                    f.write(fixed_code)
                
                print(f"✅ Fixed code written to {file_path}")
                
                branch_name = f"fix/auto-remediation-{file_path.replace('/', '-').replace('.', '-')}"
                print(f"\n🌿 Creating branch: {branch_name}")
                cloned_repo.git.checkout('-b', branch_name)
                
                cloned_repo.git.add(file_path)
                commit_message = f"Auto-fix: {', '.join(issues[:3])}"
                if len(issues) > 3:
                    commit_message += f" (+{len(issues)-3} more)"
                cloned_repo.git.commit('-m', commit_message)
                
                print(f"✅ Changes committed")
                
                print(f"📤 Pushing to GitHub...")
                origin = cloned_repo.remote('origin')
                origin.push(branch_name)
                
                print(f"✅ Branch pushed successfully")
                
                pr_body = f"""## 🤖 Automated Code Remediation

**Issues Fixed:**
{chr(10).join([f'- {issue}' for issue in issues])}

**File Modified:** `{file_path}`

---
*This PR was automatically generated by the Remediation Engine using Groq AI.*
"""
                
                print(f"\n📝 Creating Pull Request...")
                pr = repo.create_pull(
                    title=f"🤖 Auto-fix: {file_path}",
                    body=pr_body,
                    head=branch_name,
                    base=base_branch
                )
                
                print(f"\n✅ SUCCESS! Pull Request created: {pr.html_url}")
                print(f"🔗 View PR: {pr.html_url}")
                
        except Exception as e:
            print(f"\n❌ Error: {e}")
            if DEBUG_MODE:
                import traceback
                traceback.print_exc()

def main():
    """Main function to run the remediation engine"""
    print("=" * 60)
    print("🔧 MINI REMEDIATION ENGINE")
    if DEBUG_MODE:
        print("🐛 DEBUG MODE ENABLED")
    print("=" * 60)
    
    engine = RemediationEngine()
    
    repo_url = input("\n📦 Enter GitHub repository URL: ").strip()
    file_path = input("📄 Enter file path (e.g., src/utils.py): ").strip()
    base_branch = input("🌿 Enter base branch [default: main]: ").strip() or "main"
    
    engine.clone_and_fix(repo_url, file_path, base_branch)
    
    print("\n" + "=" * 60)
    print("✅ Remediation process complete!")
    print("=" * 60)

if __name__ == "__main__":
    main()
