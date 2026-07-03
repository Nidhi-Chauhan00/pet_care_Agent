import datetime
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("PetCareMCPServer")

@mcp.tool()
def search_vets(zip_code: str) -> str:
    """Search for veterinarians by zip code.
    
    Args:
        zip_code: The 5-digit zip code to search in.
    """
    return f"Here are veterinarians near {zip_code}:\n1. Happy Paws Clinic - 555-0199\n2. City Pet Hospital - 555-0210\n3. Loving Care Vet - 555-0143"

@mcp.tool()
def check_food_safety(pet_type: str, food_item: str) -> str:
    """Check if a specific food/ingredient is safe or toxic for a pet.
    
    Args:
        pet_type: Type of pet ('dog' or 'cat').
        food_item: Name of the food item (e.g. 'chocolate', 'grapes', 'apple').
    """
    toxic_dogs = ["chocolate", "grapes", "raisins", "onions", "garlic", "avocado", "xylitol"]
    toxic_cats = ["chocolate", "onions", "garlic", "grapes", "raisins", "caffeine"]
    
    item_clean = food_item.strip().lower()
    pet_clean = pet_type.strip().lower()
    
    if pet_clean == "dog":
        if item_clean in toxic_dogs:
            return f"❌ DANGER: {food_item} is highly TOXIC to dogs! Avoid feeding this."
        return f"✅ SAFE: {food_item} is generally safe for dogs in moderation."
    elif pet_clean == "cat":
        if item_clean in toxic_cats:
            return f"❌ DANGER: {food_item} is highly TOXIC to cats! Avoid feeding this."
        return f"✅ SAFE: {food_item} is generally safe for cats in moderation."
    return f"ℹ️ Info: No safety record for {food_item} for pet type '{pet_type}'."

@mcp.tool()
def log_pet_task(pet_name: str, task: str, details: str) -> str:
    """Log a completed pet care task (e.g., vaccine administered, food given, vet visit).
    
    Args:
        pet_name: Name of the pet.
        task: The task name (e.g. 'vaccine', 'feed', 'medication').
        details: Extra details of the logged task.
    """
    log_file = "pet_care_tasks.db"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {pet_name} - {task.upper()}: {details}\n"
    
    try:
        with open(log_file, "a") as f:
            f.write(log_line)
        return f"Logged task: '{task}' for {pet_name} successfully."
    except Exception as e:
        return f"Failed to log task: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
